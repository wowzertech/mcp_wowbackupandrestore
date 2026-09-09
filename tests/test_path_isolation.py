import posixpath, sys
ROOT = "aksh29@yopmail.com/"

def segment(value, label):
    if value is None: raise ValueError(f"{label} is required")
    cleaned = value.strip().strip("/").strip()
    if not cleaned: raise ValueError(f"{label} is required")
    if len(cleaned) > 255: raise ValueError(f"{label} too long")
    if cleaned.startswith("."): raise PermissionError(f"invalid {label}")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ._-()&@+")
    if not set(cleaned) <= allowed:
        raise PermissionError(f"invalid chars in {label}")
    return cleaned

def resolve(*segs):
    a = posixpath.normpath(posixpath.join(ROOT, *segs))
    if not a.startswith(ROOT): raise PermissionError("escapes")
    return a

def key_for(o,d,n): return resolve(segment(o,"org"), segment(d,"date"), segment(n,"file"))

ATTACKS = [("..","2026-05-24","a.csv"),("Test1","..","a.csv"),
 ("Test1","2026-05-24","../../victim@x.com/T/d/a.csv"),("../victim@x.com","T","a.csv"),
 ("Test1/../..","x","y"),("/","2026-05-24","a.csv"),("","2026-05-24","a.csv"),
 ("Test1","2026-05-24","/etc/passwd"),("Test1","2026-05-24","sub/dir/f.csv"),
 ("....//","2026-05-24","a.csv"),("Test1","2026-05-24","a.csv\x00.txt"),
 ("Test1%2F..%2F..","d","f"),("Test1","2026-05-24",".."),("  ..  ","d","f"),
 ("Test1","2026-05-24","..%2Faccounts.csv")]
fails=0
for a in ATTACKS:
    try:
        g=key_for(*a); print(f"  LEAK {a} -> {g}"); fails+=1
    except (PermissionError,ValueError) as e: print(f"  blocked  {str(a)[:58]:60} {type(e).__name__}")

print("\nLegitimate (must all pass):")
LEGIT=[("Test1","2026-05-24","accounts.csv"),("Test1","2026-05-24","balance_sheet.xlsx"),
 ("Acme Pty Ltd","2026-05-24","profit_and_loss.jsonl"),("R&D (NZ)","2026-01-02","tax_rates.jsonl"),
 ("Wowzer_Beta-2","2026-05-24","trial_balance.jsonl")]
for a in LEGIT:
    try:
        g=key_for(*a); assert g.startswith(ROOT); print(f"  ok  {g}")
    except Exception as e:
        print(f"  BROKE LEGIT USE {a}: {e}"); fails+=1
print(f"\n{'FAILED '+str(fails) if fails else 'All clear: traversal blocked, legitimate names pass.'}")
sys.exit(1 if fails else 0)
