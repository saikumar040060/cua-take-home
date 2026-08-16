"""In-memory data store for the mock 'Meridian Credit Union' back-office tool.

All data is synthetic. It intentionally *looks* like regulated financial data
(names, SSN fragments, account numbers) so the redaction layer has something
real to protect in logs/artifacts.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field


@dataclass
class Account:
    number: str
    product: str
    balance: str
    opened: str


@dataclass
class Member:
    member_no: str
    name: str
    ssn_last4: str
    dob: str
    phone: str
    address: str
    accounts: list[Account] = field(default_factory=list)


MEMBERS: dict[str, Member] = {
    "10023": Member(
        member_no="10023",
        name="Marisol Vega",
        ssn_last4="4417",
        dob="03/14/1987",
        phone="(313) 555-0164",
        address="482 Fenkell Ave, Detroit, MI 48238",
        accounts=[
            Account("724401-S01", "Regular Savings", "$4,812.55", "06/02/2015"),
            Account("724401-C01", "Free Checking", "$1,209.18", "06/02/2015"),
        ],
    ),
    "10456": Member(
        member_no="10456",
        name="Dmitri Okafor",
        ssn_last4="8830",
        dob="11/29/1971",
        phone="(248) 555-0122",
        address="19 Larch Ct, Royal Oak, MI 48067",
        accounts=[
            Account("731958-S01", "Regular Savings", "$22,940.03", "01/17/2009"),
        ],
    ),
    "10777": Member(
        member_no="10777",
        name="Hannah Whitfield",
        ssn_last4="2201",
        dob="07/07/1996",
        phone="(734) 555-0189",
        address="880 Packard St Apt 3, Ann Arbor, MI 48104",
        accounts=[
            Account("744102-C01", "Free Checking", "$318.77", "09/30/2021"),
        ],
    ),
}

VALID_PRODUCTS = ["Holiday Club", "Money Market", "Vacation Club", "Youth Savings"]

_ref_counter = itertools.count(1)
SUB_ACCOUNTS: list[dict] = []


def create_sub_account(member_no: str, product: str, nickname: str, deposit: str) -> str:
    ref = f"SA-2026-{next(_ref_counter):04d}"
    SUB_ACCOUNTS.append(
        {
            "ref": ref,
            "member_no": member_no,
            "product": product,
            "nickname": nickname,
            "deposit": deposit,
        }
    )
    return ref
