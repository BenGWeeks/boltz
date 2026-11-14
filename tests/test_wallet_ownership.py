from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from lnbits.core.crud import (
    create_account,
    create_wallet,
    delete_wallet,
    get_db_versions,
)
from lnbits.core.db import db as core_db
from lnbits.core.helpers import migrate_databases, run_migration

from .. import boltz_ext, migrations, views_api
from ..boltz_client.boltz import (
    BoltzReverseSwapResponse,
    BoltzSwapResponse,
    BoltzSwapStatusResponse,
)
from ..crud import (
    db,
    get_auto_reverse_submarine_swap,
    get_auto_reverse_submarine_swap_by_wallet,
    get_reverse_submarine_swaps,
    get_submarine_swaps,
)

pytestmark = pytest.mark.asyncio(loop_scope="module")

SWAP_TREE = {
    "claimLeaf": {"version": 192, "output": "51"},
    "refundLeaf": {"version": 192, "output": "52"},
}
SERVER_PK = "02" + "11" * 32
ADDRESS = "bcrt1q4vfyszl4p8cuvqh07fyhtxve5fxq8e2ux5gx43"


class FakeBoltzClient:
    def __init__(self, calls: list):
        self.calls = calls

    def substract_swap_fees(self, amount):
        return amount - 100

    def add_reverse_swap_fees(self, amount):
        return amount + 100

    async def create_swap(self, bolt11):
        self.calls.append("create_swap")
        return "refund-wif", BoltzSwapResponse(
            id="bzsub",
            expectedAmount=50_100,
            bip21="bitcoin:x",
            address="bc1paddr",
            swapTree=SWAP_TREE,
            claimPublicKey=SERVER_PK,
            acceptZeroConf=False,
            timeoutBlockHeight=850_000,
            referralId="lnbits",
        )

    async def create_reverse_swap(self, amount=0):
        self.calls.append("create_reverse_swap")
        return (
            "claim-wif",
            "ab" * 32,
            BoltzReverseSwapResponse(
                id="bzrev",
                invoice="lnbc1fake",
                swapTree=SWAP_TREE,
                refundPublicKey=SERVER_PK,
                lockupAddress="bc1plock",
                timeoutBlockHeight=850_100,
                onchainAmount=amount - 200,
                blindingKey=None,
                referralId="lnbits",
            ),
        )

    async def swap_status(self, boltz_id):
        self.calls.append("swap_status")
        return BoltzSwapStatusResponse(status="swap.created")

    async def refund_swap(self, **kwargs):
        self.calls.append("refund_swap")
        return "txid"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def setup_db():
    await migrate_databases()
    async with core_db.connect() as conn:
        versions = await get_db_versions(conn)
    current = next((v for v in versions if v.db == "boltz"), None)
    async with db.connect() as conn:
        await run_migration(conn, migrations, "boltz", current)


@pytest_asyncio.fixture(loop_scope="module")
async def calls(setup_db, monkeypatch):
    recorded: list = []

    async def fake_create_boltz_client(pair="BTC/BTC"):
        recorded.append("create_boltz_client")
        return FakeBoltzClient(recorded)

    async def fake_create_invoice(**kwargs):
        recorded.append("create_invoice")
        return SimpleNamespace(bolt11="lnbc1fake", payment_hash=uuid4().hex)

    async def fake_execute_reverse_swap(client, swap):
        recorded.append("execute_reverse_swap")

    async def fake_check_balance(data, amount=None):
        recorded.append("check_balance")
        return True

    async def fake_address_validation(address, asset):
        recorded.append("address_validation")

    monkeypatch.setattr(views_api, "boltz_client_available", lambda: True)
    monkeypatch.setattr(views_api, "create_boltz_client", fake_create_boltz_client)
    monkeypatch.setattr(views_api, "create_invoice", fake_create_invoice)
    monkeypatch.setattr(views_api, "execute_reverse_swap", fake_execute_reverse_swap)
    monkeypatch.setattr(views_api, "check_balance", fake_check_balance)
    monkeypatch.setattr(views_api, "api_address_validation", fake_address_validation)
    return recorded


@pytest_asyncio.fixture(loop_scope="module")
async def client(calls):
    app = FastAPI()
    app.include_router(boltz_ext)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


async def new_user(wallets: int = 1):
    account = await create_account()
    return [
        await create_wallet(user_id=account.id, wallet_name=f"w{i}")
        for i in range(wallets)
    ]


def headers(wallet):
    return {"X-Api-Key": wallet.adminkey}


def swap_body(wallet_id):
    return {"wallet": wallet_id, "refund_address": ADDRESS, "amount": 50_000}


def reverse_body(wallet_id):
    return {
        "wallet": wallet_id,
        "instant_settlement": True,
        "onchain_address": ADDRESS,
        "amount": 20_000,
    }


async def no_swaps_for(wallet_id):
    assert await get_submarine_swaps([wallet_id]) == []
    assert await get_reverse_submarine_swaps([wallet_id]) == []
    assert await get_auto_reverse_submarine_swap_by_wallet(wallet_id) is None


async def test_create_on_own_first_wallet(client, calls):
    (w1,) = await new_user()
    r = await client.post(
        "/boltz/api/v1/swap", json=swap_body(w1.id), headers=headers(w1)
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        "/boltz/api/v1/swap/reverse", json=reverse_body(w1.id), headers=headers(w1)
    )
    assert r.status_code == 201, r.text
    assert "execute_reverse_swap" in calls


async def test_create_on_own_second_wallet_with_first_wallet_key(client, calls):
    w1, w2 = await new_user(2)
    r = await client.post(
        "/boltz/api/v1/swap", json=swap_body(w2.id), headers=headers(w1)
    )
    assert r.status_code == 201, r.text
    assert r.json()["wallet"] == w2.id
    r = await client.post(
        "/boltz/api/v1/swap/reverse", json=reverse_body(w2.id), headers=headers(w1)
    )
    assert r.status_code == 201, r.text
    assert r.json()["wallet"] == w2.id
    r = await client.post(
        "/boltz/api/v1/swap/reverse/auto",
        json=reverse_body(w2.id),
        headers=headers(w1),
    )
    assert r.status_code == 201, r.text
    assert r.json()["wallet"] == w2.id


async def test_create_on_other_users_wallet_is_forbidden(client, calls):
    (attacker,) = await new_user()
    (victim,) = await new_user()
    for path, body in [
        ("/boltz/api/v1/swap", swap_body(victim.id)),
        ("/boltz/api/v1/swap/reverse", reverse_body(victim.id)),
        ("/boltz/api/v1/swap/reverse/auto", reverse_body(victim.id)),
    ]:
        r = await client.post(path, json=body, headers=headers(attacker))
        assert r.status_code == 403, (path, r.text)
    assert calls == []
    await no_swaps_for(victim.id)


async def test_create_on_unknown_or_deleted_wallet_is_forbidden(client, calls):
    w1, deleted = await new_user(2)
    await delete_wallet(user_id=w1.user, wallet_id=deleted.id)
    for wallet_id in [uuid4().hex, deleted.id]:
        for path, body in [
            ("/boltz/api/v1/swap", swap_body(wallet_id)),
            ("/boltz/api/v1/swap/reverse", reverse_body(wallet_id)),
            ("/boltz/api/v1/swap/reverse/auto", reverse_body(wallet_id)),
        ]:
            r = await client.post(path, json=body, headers=headers(w1))
            assert r.status_code == 403, (path, r.text)
        await no_swaps_for(wallet_id)
    assert calls == []


async def test_swap_id_endpoints_hide_other_users_swaps(client, calls):
    (owner,) = await new_user()
    (other,) = await new_user()
    r = await client.post(
        "/boltz/api/v1/swap", json=swap_body(owner.id), headers=headers(owner)
    )
    swap_id = r.json()["id"]
    r = await client.post(
        "/boltz/api/v1/swap/reverse",
        json=reverse_body(owner.id),
        headers=headers(owner),
    )
    reverse_id = r.json()["id"]
    r = await client.post(
        "/boltz/api/v1/swap/reverse/auto",
        json=reverse_body(owner.id),
        headers=headers(owner),
    )
    auto_id = r.json()["id"]
    calls.clear()

    unknown = uuid4().hex
    requests = [
        ("post", "/boltz/api/v1/swap/refund?swap_id={}", swap_id),
        ("post", "/boltz/api/v1/swap/status?swap_id={}", swap_id),
        ("post", "/boltz/api/v1/swap/status?swap_id={}", reverse_id),
        ("delete", "/boltz/api/v1/swap/reverse/auto/{}", auto_id),
    ]
    for method, path, existing_id in requests:
        foreign = await client.request(
            method, path.format(existing_id), headers=headers(other)
        )
        missing = await client.request(
            method, path.format(unknown), headers=headers(other)
        )
        assert foreign.status_code == missing.status_code == 404, path
        assert foreign.json() == missing.json(), path
    assert calls == []
    assert await get_auto_reverse_submarine_swap(auto_id) is not None
    assert (await get_submarine_swaps([owner.id]))[0].status == "pending"

    r = await client.post(
        f"/boltz/api/v1/swap/status?swap_id={reverse_id}", headers=headers(owner)
    )
    assert r.status_code == 200, r.text
    r = await client.post(
        f"/boltz/api/v1/swap/refund?swap_id={swap_id}", headers=headers(owner)
    )
    assert r.status_code == 200, r.text
    r = await client.delete(
        f"/boltz/api/v1/swap/reverse/auto/{auto_id}", headers=headers(owner)
    )
    assert r.status_code == 200, r.text
    assert await get_auto_reverse_submarine_swap(auto_id) is None


async def test_list_endpoints_only_return_the_users_swaps(client, calls):
    w1, w2 = await new_user(2)
    (other,) = await new_user()
    for wallet in [w2, other]:
        await client.post(
            "/boltz/api/v1/swap", json=swap_body(wallet.id), headers=headers(wallet)
        )
        await client.post(
            "/boltz/api/v1/swap/reverse",
            json=reverse_body(wallet.id),
            headers=headers(wallet),
        )
        await client.post(
            "/boltz/api/v1/swap/reverse/auto",
            json=reverse_body(wallet.id),
            headers=headers(wallet),
        )
    for path in [
        "/boltz/api/v1/swap",
        "/boltz/api/v1/swap/reverse",
        "/boltz/api/v1/swap/reverse/auto",
    ]:
        r = await client.get(f"{path}?all_wallets=true", headers=headers(w1))
        assert [s["wallet"] for s in r.json()] == [w2.id], path
        r = await client.get(path, headers=headers(w1))
        assert r.json() == [], path
