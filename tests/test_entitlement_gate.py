"""Entitlement gate: inactive subscriptions and org scoping must both hold."""
import sys
from dataclasses import dataclass

class SubscriptionInactive(PermissionError): pass

@dataclass(frozen=True)
class Entitlement:
    account_id: str; s3_prefix: str; plan: str; active: bool
    organisations: tuple | None = None
    def check(self):
        if not self.active:
            raise SubscriptionInactive("no active subscription")
    def permits_organisation(self, name):
        return True if self.organisations is None else name in self.organisations

fails = 0
def case(label, fn, should_raise):
    global fails
    try:
        fn(); ok = not should_raise
        print(f"  {'ok' if ok else 'FAIL'}  {label}: allowed")
    except PermissionError as e:
        ok = should_raise
        print(f"  {'ok' if ok else 'FAIL'}  {label}: blocked ({type(e).__name__})")
    if not ok: fails += 1

lapsed = Entitlement("acct_1","a@x.com/","pro",active=False)
live   = Entitlement("acct_2","b@x.com/","pro",active=True)
scoped = Entitlement("acct_3","c@x.com/","free",active=True,organisations=("Test1",))

case("lapsed subscription", lapsed.check, should_raise=True)
case("live subscription",   live.check,   should_raise=False)

def org(ent, name):
    if not ent.permits_organisation(name):
        raise PermissionError(f"not entitled to {name}")

case("live acct, any org",           lambda: org(live,"Anything"),   should_raise=False)
case("scoped acct, permitted org",   lambda: org(scoped,"Test1"),    should_raise=False)
case("scoped acct, other org",       lambda: org(scoped,"Test2"),    should_raise=True)
case("scoped acct, victim org",      lambda: org(scoped,"AcmeCorp"), should_raise=True)

print(f"\n{'FAILED ' + str(fails) if fails else 'Entitlement gate holds.'}")
sys.exit(1 if fails else 0)
