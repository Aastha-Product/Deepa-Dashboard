"""One-time Zoho Inventory setup (read-only), India data centre.

Before running:
  1. Open https://api-console.zoho.in  ->  Add Client  ->  Self Client  ->  Create.
  2. On the Self Client's "Generate Code" tab, paste this scope list, choose 10 minutes, add a
     description and click Create:
        ZohoInventory.purchaseorders.READ,ZohoInventory.items.READ,ZohoInventory.settings.READ
  3. Keep the Client ID, Client Secret and the generated code to hand (the code expires in 10 minutes).

Then, from the project folder:
    .venv\\Scripts\\python tools\\zoho_setup.py

Secrets are typed into hidden prompts and never printed. The script writes:
  - the four ZOHO_ values into .env (for local runs), and
  - zoho.vercel.env, for Vercel -> deepa-dashboard -> Settings -> Environment Variables -> Import .env.
    Delete that file after importing it. It's git-ignored.
"""
import getpass
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
ACCOUNTS = "https://accounts.zoho.in"
API = "https://www.zohoapis.in"


def ask(prompt: str, secret: bool = True) -> str:
    value = (getpass.getpass if secret else input)(prompt).strip()
    if not value:
        sys.exit("Nothing entered; stopping. Nothing was changed.")
    return value


def set_env(path: Path, values: dict) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    for key, value in values.items():
        line = f"{key}={value}"
        text, n = re.subn(rf"(?m)^{key}=.*$", lambda _: line, text)
        if not n:
            text = text.rstrip("\n") + f"\n{line}\n"
    path.write_text(text, encoding="utf-8")


def main() -> int:
    print("Zoho Inventory setup (read-only, India data centre). Input is hidden; paste and press Enter.\n")
    client_id = ask("Client ID: ")
    client_secret = ask("Client Secret: ")
    code = ask("Generated code (expires in 10 minutes): ")

    with httpx.Client(timeout=30) as c:
        r = c.post(f"{ACCOUNTS}/oauth/v2/token", data={"grant_type": "authorization_code", "code": code,
                                                         "client_id": client_id, "client_secret": client_secret})
        body = r.json()
        if "refresh_token" not in body:
            sys.exit(f"Zoho didn't return a refresh token ({body.get('error', r.status_code)}). "
                     "Generate a new code (they expire after 10 minutes) and run this again.")
        refresh, access = body["refresh_token"], body["access_token"]
        headers = {"Authorization": f"Zoho-oauthtoken {access}"}

        r = c.get(f"{API}/inventory/v1/organizations", headers=headers)
        orgs = r.json().get("organizations", []) if r.status_code == 200 else []
        if not orgs:
            org_id = ask("Couldn't list organisations. Organisation ID (Zoho Inventory -> Settings -> "
                         "Organization Profile): ", secret=False)
        elif len(orgs) == 1:
            org_id = str(orgs[0]["organization_id"])
            print(f"Organisation: {orgs[0].get('name')} ({org_id})")
        else:
            for i, o in enumerate(orgs, 1):
                print(f"  {i}. {o.get('name')} ({o['organization_id']})")
            org_id = str(orgs[int(ask('Which number is Freshroots? ', secret=False)) - 1]["organization_id"])

        checks = {}
        for path, key in (("items", "items"), ("purchaseorders", "purchaseorders")):
            r = c.get(f"{API}/inventory/v1/{path}", headers=headers,
                      params={"organization_id": org_id, "page": 1, "per_page": 200})
            checks[path] = (r.status_code, len(r.json().get(key, [])) if r.status_code == 200 else None)
        for path, (status, n) in checks.items():
            print(f"Read {path}: " + (f"OK, {n} on the first page" if status == 200 else f"failed (HTTP {status})"))
        if any(status != 200 for status, _ in checks.values()):
            sys.exit("Zoho refused a read. Check the scopes on the Self Client and run this again.")

        skus = {i.get("sku") for i in c.get(f"{API}/inventory/v1/items", headers=headers,
                                             params={"organization_id": org_id, "per_page": 200}).json().get("items", [])}
        expected = {f"SKU{n:03d}" for n in range(1, 16)}
        print(f"Freshroots SKUs found in Zoho items: {len(expected & skus)} of 15"
              + ("" if expected <= skus else " (item SKUs in Zoho must match SKU001…SKU015 for open POs to count)"))

    values = {"ZOHO_CLIENT_ID": client_id, "ZOHO_CLIENT_SECRET": client_secret, "ZOHO_REFRESH_TOKEN": refresh,
              "ZOHO_ORGANIZATION_ID": org_id, "ZOHO_ACCOUNTS_URL": ACCOUNTS, "ZOHO_API_URL": API}
    set_env(ROOT / ".env", values)
    (ROOT / "zoho.vercel.env").write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    print("\nSaved to .env, and to zoho.vercel.env for Vercel.\n"
          "Next: Vercel -> deepa-dashboard -> Settings -> Environment Variables -> Import .env -> choose\n"
          f"{ROOT / 'zoho.vercel.env'}  -> Save, then redeploy. Delete zoho.vercel.env afterwards.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
