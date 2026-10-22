"""
Debug exact Flutterwave message for 7049215685
Put next to manage.py, live keys already in .env

PS> python DEBUG_FLW_RESOLVE.py
"""
import os; os.environ.setdefault("DJANGO_SETTINGS_MODULE","Artisan_platform.api_settings")
import django
try: django.setup()
except:
    os.environ["DJANGO_SETTINGS_MODULE"]="Artisan_platform.settings"
    django.setup()

import requests, json
from django.conf import settings
from payments.payout_accounts import get_supported_nigerian_banks

acct = "7049215685"
print(f"Testing account: {acct}")
print(f"FLW_BASE={settings.FLW_API_BASE_URL}")
print(f"FLW_KEY=***{settings.FLW_SECRET_KEY[-8:]}" if getattr(settings,'FLW_SECRET_KEY','') else "MISSING")

# get banks to see codes
try:
    banks = get_supported_nigerian_banks(force_refresh=True)
    print(f"\nBanks loaded: {len(banks)}")
    # find common banks for this account - ask user which they picked
    for b in banks:
        if "opal" in b['name'].lower() or "access" in b['name'].lower() or "gtb" in b['name'].lower() or "fidelity" in b['name'].lower():
            print(f"  {b['code']} {b['name']}")
except Exception as e:
    print(f"banks error: {e}")

# Try direct Flutterwave call WITH full logging
bank_code = input("\nEnter the bank_code you selected in the UI (from dropdown): ").strip()
if not bank_code:
    # try guess OPay/PalmPay codes which are often filtered
    print("No code given - trying 3 common codes for 7049 prefix (OPay/PalmPay): 999992, 100004, 999991")
    candidates=[bank_code] if bank_code else ["999992","100004","999991","044","058","011","032"]
else:
    candidates=[bank_code]

for code in candidates:
    print(f"\n---Trying {acct} + bank_code={code} ---")
    try:
        url=f"{settings.FLW_API_BASE_URL.rstrip('/')}/accounts/resolve"
        headers={"Authorization":f"Bearer {settings.FLW_SECRET_KEY}","Content-Type":"application/json"}
        payload={"account_number":acct,"account_bank":code}
        print(f"POST {url}")
        print(f"Body: {json.dumps(payload)}")
        r=requests.post(url, headers=headers, json=payload, timeout=15)
        print(f"Status: {r.status_code}")
        try:
            data=r.json()
            print(f"Response: {json.dumps(data, indent=2)}")
            if data.get("status")=="success":
                print(f"✅ SUCCESS - account_name={data.get('data',{}).get('account_name')}")
                break
            else:
                print(f"❌ Flutterwave says: {data.get('message')}")
        except:
            print(f"Raw: {r.text[:500]}")
    except Exception as e:
        print(f"Request failed: {e}")

print("\n--- Common reasons for this 400 even with live keys ---")
print("1. Bank code wrong for this number - 7049... is typically OPay(999992)/PalmPay(100004)/Moniepoint(50515) - all are MOBILE_MONEY and FILTERED OUT in payout_accounts.py:35")
print("   -> Check: if your bank not in /banks/ 200 list, payout_accounts.py intentionally blocks it")
print("2. Flutterwave business not approved for transfers - Dashboard > Settings > Payouts must be enabled")
print("3. Account is not NUBAN (wallet ID) - some fintechs use 10-digit but Flutterwave resolve only supports DMBs")
print("\nIf OPay/PalmPay: you CANNOT save it as payout yet - repo blocks mobile_money. Tell user to use DMB (Access 044, GTB 058, Fidelity 070, etc)")
