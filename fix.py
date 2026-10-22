"""
Windows / PowerShell - run DIRECTLY
Place INSIDE Artisan_backend/ (next to manage.py):
  PS> python FIX_PAYOUT_WINDOWS_v2.py
  PS> python FIX_PAYOUT_WINDOWS_v2.py kehinde@example.com
"""
import os, sys
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "Artisan_platform.api_settings")
import django
try:
    django.setup()
except Exception as e:
    os.environ["DJANGO_SETTINGS_MODULE"] = "Artisan_platform.settings"
    try:
        django.setup()
    except Exception as e2:
        print(f"Can't setup Django: {e} / {e2}")
        print("Put this file next to manage.py inside Artisan_backend/")
        sys.exit(1)

# FIXED imports - ArtisanVerification lives in services.models
from home.models import ArtisanProfile, CustomUser
from services.models import ArtisanVerification
from services.kyc_crypto import encrypt_identity_name, decrypt_identity_name
from services.identity_names import profile_name_matches_identity, PROFILE_NAME_CORRECTION_MESSAGE

def ask(p):
    try: return input(p).strip()
    except: return ""

EMAIL = next((a for a in sys.argv[1:] if "@" in a), None)
if not EMAIL:
    EMAIL = ask("Artisan email: ")
if not EMAIL or "@" not in EMAIL:
    print("Usage: python FIX_PAYOUT_WINDOWS_v2.py you@example.com")
    sys.exit(1)

user = CustomUser.objects.filter(email__iexact=EMAIL).first()
if not user:
    print(f"❌ No user {EMAIL}"); sys.exit(1)
artisan = getattr(user, "artisan_profile", None)
if not artisan:
    print(f"❌ {EMAIL} has no artisan_profile (is CUSTOMER)"); sys.exit(1)

ver = ArtisanVerification.objects.filter(artisan=artisan).first()
print(f"\nUser: {user.email} | {user.first_name} {user.last_name} | active={user.is_active}")
print(f"Artisan: status={artisan.profile_status} country={artisan.country} state={artisan.state}")
if ver:
    print(f"Verification: status={ver.verification_status} tier={ver.verification_tier} encrypted={'SET' if ver.verified_identity_name_encrypted else 'EMPTY'}")
    print(f"              provider={ver.verification_provider} ref={ver.verification_reference} at={ver.verified_at}")
else:
    print("No row - creating UNVERIFIED")
    ver = ArtisanVerification.objects.create(artisan=artisan, verification_status="UNVERIFIED", verification_tier=1)

if ver.verification_status == "VERIFIED" and not ver.verified_identity_name_encrypted:
    print("\n⚠️ VERIFIED but encrypted EMPTY -> admin manually flipped status, payout will block")

if ver.verified_identity_name_encrypted:
    try:
        ident = decrypt_identity_name(ver.verified_identity_name_encrypted)
        print(f"\nDecrypted: {ident}")
        ok = profile_name_matches_identity(ident, user.first_name, user.last_name)
        print(f"profile_name_matches_identity -> {ok}")
        if not ok: print(f"⚠️ MISMATCH: {PROFILE_NAME_CORRECTION_MESSAGE}")
    except Exception as e: print(f"Decrypt failed: {e}")

if artisan.profile_status != "ACTIVE":
    print(f"\n⚠️ profile_status={artisan.profile_status} must be ACTIVE")

print("\n--- FIX ---")
print("1) Set profile name to match NIN/BVN")
print("2) Encrypt & set verified_identity_name")
print("3) Set profile ACTIVE")
print("4) Diagnose only")
choice = ask("Choose 1/2/3/4 (e.g. 1,3): ")

if "1" in choice:
    if ver.verified_identity_name_encrypted:
        try:
            ident = decrypt_identity_name(ver.verified_identity_name_encrypted)
            use = ask(f"Set to first='{ident['first_name'].split()[0]}' last='{ident['last_name']}' ? y/n: ").lower()
            if use=="y":
                first=ident['first_name'].split()[0].title(); last=ident['last_name'].title()
                user.first_name=first; user.last_name=last
                user.save(update_fields=["first_name","last_name"])
                print(f"✅ {first} {last}")
            else:
                first=ask("first_name: "); last=ask("last_name: ")
                if first and last:
                    user.first_name=first; user.last_name=last
                    user.save(update_fields=["first_name","last_name"])
                    print(f"✅ {first} {last}")
        except Exception as e:
            print(e)
            first=ask("first_name: "); last=ask("last_name: ")
            if first and last:
                user.first_name=first; user.last_name=last
                user.save(update_fields=["first_name","last_name"])
                print(f"✅ {first} {last}")
    else:
        first=ask("first_name: "); last=ask("last_name: ")
        if first and last:
            user.first_name=first; user.last_name=last
            user.save(update_fields=["first_name","last_name"])
            print(f"✅ {first} {last}")

if "2" in choice:
    print("\nEnter EXACT NIN/BVN name:")
    fn=ask("verified first_name: "); mn=ask("middle_name (empty ok): "); ln=ask("verified last_name: ")
    if fn and ln:
        ident={"first_name":fn,"middle_name":mn,"last_name":ln}
        ok=profile_name_matches_identity(ident, user.first_name, user.last_name)
        print(f"check -> {ok}")
        if not ok: print("⚠️ Still mismatch - do 1 first")
        else:
            ver.verified_identity_name_encrypted=encrypt_identity_name(ident)
            ver.verification_status="VERIFIED"
            from django.utils import timezone; ver.verified_at=timezone.now()
            if not ver.verification_provider: ver.verification_provider="mono"
            ver.save(update_fields=["verified_identity_name_encrypted","verification_status","verified_at","verification_provider","updated_at"])
            print("✅ Encrypted + VERIFIED")

if "3" in choice:
    artisan.profile_status="ACTIVE"
    artisan.save(update_fields=["profile_status","updated_at"])
    print("✅ ACTIVE")

print("\nDone - retry Payout Account. Check GET /api/v1/services/verification/ -> VERIFIED + identity_name_available:true")
