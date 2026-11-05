import json
import re

from django import forms
from .geography import canonical_location
from .profile_catalogue import initialize_profile_location, clean_profile_location
from django.utils import timezone

from .phone_fields import (
    DEFAULT_CALLING_CODE,
    calling_code_choices,
    clean_phone_fields,
    initialize_phone_fields,
)
from .profile_links import (
    SOCIAL_MEDIA_PLATFORM_CHOICES,
    clean_profile_links,
)

INPUT_CLASS = "field-input"
_MEANINGFUL_WORD = re.compile(r"[^\W\d_]+(?:[’'-][^\W\d_]+)*", re.UNICODE)
SUPPORT_DESCRIPTION_ERROR = "Description must contain at least six meaningful words."


def _normalize_support_description(value):
    description = str(value or "").strip()
    if len(_MEANINGFUL_WORD.findall(description)) < 6:
        raise forms.ValidationError(SUPPORT_DESCRIPTION_ERROR)
    return description


def _location_records(locations):
    records = []
    for item in locations or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        record = {
            key: str(item.get(key, "") or "").strip()
            for key in ("country", "state", "area")
        }
        record["id"] = str(item["id"])
        records.append(record)
    return records


def _apply_location_controls(form, locations):
    records = _location_records(locations)
    form.fields["location_id"].widget.attrs["data-location-select"] = ""
    form.fields["location_id"].widget.attrs["data-location-records"] = json.dumps(
        records, ensure_ascii=False
    )
    current = str(form.initial.get("location_id") or "")
    if current:
        chosen = next((item for item in records if item["id"] == current), None)
        if chosen:
            form.initial.setdefault("location_country_filter", chosen["country"])
            form.initial.setdefault("location_state_filter", chosen["state"])
            if "location_area_filter" in form.fields:
                form.initial.setdefault("location_area_filter", chosen["area"])
    form.initial.setdefault("location_country_filter", "Nigeria")
    if "location_id" in form.fields and form.fields["location_id"].widget.attrs.get(
        "data-location-anywhere-value"
    ):
        form.fields["location_id"].widget.attrs[
            "data-location-anywhere-value"
        ] = "__anywhere__"


class StyledForm(forms.Form):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            existing = field.widget.attrs.get("class", "")
            field.widget.attrs["class"] = f"{existing} {INPUT_CLASS}".strip()
            field.widget.attrs.setdefault("autocomplete", "off")


class RegisterForm(StyledForm):
    email = forms.EmailField()
    first_name = forms.CharField(max_length=40)
    last_name = forms.CharField(max_length=40)
    password = forms.CharField(min_length=8, widget=forms.PasswordInput)
    role = forms.ChoiceField(choices=[("customer", "Customer"), ("artisan", "Artisan")])


class LoginForm(StyledForm):
    email = forms.EmailField(
        widget=forms.EmailInput(
            attrs={
                "autocomplete": "username",
                "autocapitalize": "none",
                "spellcheck": "false",
            }
        )
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"})
    )


class OTPForm(StyledForm):
    email = forms.EmailField(widget=forms.EmailInput(attrs={"autocomplete": "email"}))
    otp = forms.RegexField(
        regex=r"^\d{6}$",
        max_length=6,
        min_length=6,
        widget=forms.TextInput(
            attrs={"autocomplete": "one-time-code", "inputmode": "numeric"}
        ),
    )


class StaffMFAVerifyForm(StyledForm):
    code = forms.RegexField(
        regex=r"^\d{6}$",
        max_length=6,
        min_length=6,
        label="Verification code",
        widget=forms.TextInput(
            attrs={"autocomplete": "one-time-code", "inputmode": "numeric"}
        ),
    )


class EmailForm(StyledForm):
    email = forms.EmailField(widget=forms.EmailInput(attrs={"autocomplete": "email"}))


class ResetForm(StyledForm):
    new_password = forms.CharField(
        min_length=8,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    confirm_password = forms.CharField(
        min_length=8,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    def clean(self):
        cleaned = super().clean()
        if (
            cleaned.get("new_password")
            and cleaned.get("confirm_password")
            and cleaned["new_password"] != cleaned["confirm_password"]
        ):
            self.add_error("confirm_password", "Passwords do not match.")
        return cleaned


class StaffRecoveryVerifyForm(StyledForm):
    email = forms.EmailField(widget=forms.EmailInput(attrs={"autocomplete": "email"}))
    otp = forms.RegexField(
        regex=r"^\d{6}$",
        max_length=6,
        min_length=6,
        widget=forms.TextInput(
            attrs={"autocomplete": "one-time-code", "inputmode": "numeric"}
        ),
    )
    question_1 = forms.ChoiceField(choices=[])
    answer_1 = forms.CharField(
        min_length=3,
        max_length=160,
        widget=forms.TextInput(attrs={"autocomplete": "off"}),
    )
    question_2 = forms.ChoiceField(choices=[])
    answer_2 = forms.CharField(
        min_length=3,
        max_length=160,
        widget=forms.TextInput(attrs={"autocomplete": "off"}),
    )
    question_3 = forms.ChoiceField(choices=[])
    answer_3 = forms.CharField(
        min_length=3,
        max_length=160,
        widget=forms.TextInput(attrs={"autocomplete": "off"}),
    )

    def __init__(self, *args, question_choices=(), **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("question_1", "question_2", "question_3"):
            self.fields[name].choices = list(question_choices)

    def clean(self):
        cleaned = super().clean()
        ids = [cleaned.get(f"question_{index}") for index in range(1, 4)]
        if None not in ids and len(set(ids)) != 3:
            raise forms.ValidationError("Choose three different security questions.")
        return cleaned


class StaffSecuritySetupForm(StaffRecoveryVerifyForm):
    pass


class StaffResetCompleteForm(ResetForm):
    pass


class StaffPasswordChangeForm(ResetForm):
    current_password = forms.CharField(
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.order_fields(["current_password", "new_password", "confirm_password"])


class StaffSecurityQuestionUpdateForm(StaffRecoveryVerifyForm):
    email = None
    otp = None
    current_password = forms.CharField(
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"})
    )
    current_question_1 = forms.ChoiceField(
        choices=[], widget=forms.HiddenInput(), required=False
    )
    current_answer_1 = forms.CharField(
        min_length=3, max_length=160, required=False, label="Current answer 1"
    )
    current_question_2 = forms.ChoiceField(
        choices=[], widget=forms.HiddenInput(), required=False
    )
    current_answer_2 = forms.CharField(
        min_length=3, max_length=160, required=False, label="Current answer 2"
    )
    current_question_3 = forms.ChoiceField(
        choices=[], widget=forms.HiddenInput(), required=False
    )
    current_answer_3 = forms.CharField(
        min_length=3, max_length=160, required=False, label="Current answer 3"
    )

    def __init__(self, *args, question_choices=(), registered_questions=None, **kwargs):
        super().__init__(*args, question_choices=question_choices, **kwargs)
        self.fields.pop("email", None)
        self.fields.pop("otp", None)
        prompt_labels = dict(question_choices)
        for index, question_id in enumerate(registered_questions or (), start=1):
            field = self.fields[f"current_question_{index}"]
            field.choices = list(question_choices)
            self.initial.setdefault(field.name, question_id)
            self.fields[f"current_answer_{index}"].label = (
                f"Current answer · {prompt_labels.get(question_id, 'registered question')}"
            )
        if not registered_questions:
            for index in range(1, 4):
                self.fields.pop(f"current_question_{index}")
                self.fields.pop(f"current_answer_{index}")
        field_order = ["current_password"]
        if registered_questions:
            for index in range(1, 4):
                field_order.extend(
                    [f"current_question_{index}", f"current_answer_{index}"]
                )
        for index in range(1, 4):
            field_order.extend([f"question_{index}", f"answer_{index}"])
        self.order_fields(field_order)

    def clean(self):
        cleaned = super().clean()
        if "current_question_1" in self.fields:
            current_ids = [
                cleaned.get(f"current_question_{index}") for index in range(1, 4)
            ]
            current_answers = [
                cleaned.get(f"current_answer_{index}") for index in range(1, 4)
            ]
            if (
                not all(current_ids)
                or len(set(current_ids)) != 3
                or not all(current_answers)
            ):
                raise forms.ValidationError(
                    "Enter all three current answers before replacing security questions."
                )
        return cleaned


class CustomerProfileForm(StyledForm):
    first_name = forms.CharField(max_length=40)
    last_name = forms.CharField(max_length=40)
    phone_country_calling_code = forms.ChoiceField(
        choices=calling_code_choices(),
        required=False,
        initial=DEFAULT_CALLING_CODE,
        widget=forms.Select(attrs={"data-phone-calling-code": ""}),
    )
    phone_number = forms.CharField(
        max_length=30,
        required=False,
        label="Phone number",
        help_text="Choose a calling code. Nigerian numbers work with or without the leading 0.",
        widget=forms.TextInput(
            attrs={
                "data-phone-number": "",
                "autocomplete": "tel-national",
                "inputmode": "tel",
            }
        ),
    )
    date_of_birth = forms.DateField(
        required=False,
        widget=forms.DateInput(
            format="%Y-%m-%d",
            attrs={"type": "text", "placeholder": "YYYY-MM-DD", "autocomplete": "bday"},
        ),
    )
    country = forms.CharField(
        max_length=100,
        required=False,
        widget=forms.TextInput(),
    )
    state = forms.CharField(
        label="State or region",
        max_length=100,
        required=False,
        widget=forms.TextInput(),
    )
    area = forms.CharField(
        label="Area or locality",
        max_length=150,
        required=False,
        help_text="Choose your area or locality from the shared worldwide catalogue.",
    )
    nearest_institution = forms.CharField(
        max_length=180,
        required=False,
        label="Nearest institution",
        help_text="Search institutions worldwide; location filters are optional. Choosing one fills its recorded location.",
        widget=forms.TextInput(attrs={"data-institution-name": ""}),
    )
    nearest_institution_abbreviation = forms.CharField(
        max_length=160, required=False, widget=forms.HiddenInput()
    )
    nearest_institution_source_id = forms.CharField(
        max_length=100, required=False, widget=forms.HiddenInput()
    )
    gender = forms.ChoiceField(
        choices=[("M", "Male"), ("F", "Female"), ("N", "Prefer not to say")]
    )
    profile_photo = forms.ImageField(
        required=False, help_text="Optional profile photo for service conversations."
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        initialize_phone_fields(self)
        initialize_profile_location(self)

    def clean_date_of_birth(self):
        value = self.cleaned_data.get("date_of_birth")
        if value and value > timezone.localdate():
            raise forms.ValidationError("Date of birth cannot be in the future.")
        return value

    def clean(self):
        return clean_profile_location(self, clean_phone_fields(self, super().clean()))


class ArtisanProfileForm(StyledForm):
    first_name = forms.CharField(max_length=40)
    last_name = forms.CharField(max_length=40)
    phone_country_calling_code = forms.ChoiceField(
        choices=calling_code_choices(),
        required=False,
        initial=DEFAULT_CALLING_CODE,
        widget=forms.Select(attrs={"data-phone-calling-code": ""}),
    )
    phone_number = forms.CharField(
        max_length=30,
        required=False,
        label="Phone number",
        help_text="Choose a calling code. Nigerian numbers work with or without the leading 0.",
        widget=forms.TextInput(
            attrs={
                "data-phone-number": "",
                "autocomplete": "tel-national",
                "inputmode": "tel",
            }
        ),
    )
    business_name = forms.CharField(max_length=150, required=False)
    bio = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 4}))
    years_experience = forms.IntegerField(min_value=0, required=False)
    country = forms.CharField(
        max_length=100,
        required=False,
        widget=forms.TextInput(),
    )
    state = forms.CharField(
        label="State or region",
        max_length=100,
        required=False,
        widget=forms.TextInput(),
    )
    area = forms.CharField(
        label="Area or locality",
        max_length=150,
        required=False,
        help_text="Choose your locality from the shared worldwide catalogue.",
    )
    nearest_institution = forms.CharField(
        max_length=180,
        required=False,
        label="Nearest institution",
        help_text="Search institutions worldwide; location filters are optional. Choosing one fills its recorded location.",
    )
    nearest_institution_abbreviation = forms.CharField(
        max_length=160, required=False, widget=forms.HiddenInput()
    )
    nearest_institution_source_id = forms.CharField(
        max_length=100, required=False, widget=forms.HiddenInput()
    )
    profile_photo = forms.ImageField(required=False)
    social_media_1_platform = forms.ChoiceField(
        label="Social Media Link 1 platform",
        choices=[("", "Select a platform")] + list(SOCIAL_MEDIA_PLATFORM_CHOICES),
        required=False,
    )
    social_media_1_url = forms.CharField(
        label="Social Media Link 1",
        max_length=2048,
        required=False,
        help_text="Enter a professional social media profile that demonstrates your work.",
        widget=forms.URLInput(attrs={"placeholder": "https://…"}),
    )
    social_media_2_platform = forms.ChoiceField(
        label="Social Media Link 2 platform",
        choices=[("", "Select a platform")] + list(SOCIAL_MEDIA_PLATFORM_CHOICES),
        required=False,
    )
    social_media_2_url = forms.CharField(
        label="Social Media Link 2",
        max_length=2048,
        required=False,
        help_text="Add another relevant professional social media profile, if available.",
        widget=forms.URLInput(attrs={"placeholder": "https://…"}),
    )
    portfolio_url = forms.CharField(
        label="Portfolio or Website",
        max_length=2048,
        required=False,
        help_text="Add your portfolio, professional website or a page containing examples of your work.",
        widget=forms.URLInput(attrs={"placeholder": "https://…"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        initialize_phone_fields(self)
        initialize_profile_location(self)

    def clean(self):
        cleaned = clean_phone_fields(self, super().clean())
        return clean_profile_location(self, clean_profile_links(self, cleaned))


class PromotionApplicationForm(StyledForm):
    promotion_type = forms.ChoiceField(
        label="Promotion product",
        choices=[
            ("", "Choose a promotion product"),
            ("INSTITUTION", "Institution Promotion"),
            ("AREA", "Area Promotion"),
            ("STATE", "State Promotion"),
            ("HOMEPAGE", "Homepage Feature"),
        ],
        required=True,
        widget=forms.Select(attrs={"data-promotion-type": ""}),
    )
    institution_id = forms.ChoiceField(
        label="Institution",
        choices=[("", "Choose an institution")],
        required=False,
        widget=forms.Select(attrs={"data-promotion-target": "INSTITUTION"}),
    )
    area_location_id = forms.ChoiceField(
        label="Area or locality",
        choices=[("", "Choose a supported area")],
        required=False,
        widget=forms.Select(attrs={"data-promotion-target": "AREA"}),
    )
    state_country = forms.ChoiceField(
        label="Country",
        choices=[("", "Choose a country")],
        required=False,
        widget=forms.Select(
            attrs={"data-promotion-target": "STATE", "data-promotion-country": ""}
        ),
    )
    state_name = forms.ChoiceField(
        label="State or region",
        choices=[("", "Choose a state or region")],
        required=False,
        widget=forms.Select(
            attrs={"data-promotion-target": "STATE", "data-promotion-state": ""}
        ),
    )
    service_ids = forms.MultipleChoiceField(
        label="Active service offerings",
        choices=[],
        required=True,
        help_text="Choose the active services that match this target. General/anywhere offerings remain valid.",
        widget=forms.SelectMultiple(attrs={"size": "5"}),
    )
    promotional_description = forms.CharField(
        label="Promotion description",
        min_length=40,
        max_length=1200,
        help_text="Describe your work and why this promotion target is relevant. Do not include identity numbers or sensitive information.",
        widget=forms.Textarea(attrs={"rows": 5, "maxlength": 1200}),
    )

    def __init__(self, *args, institutions=(), locations=(), offerings=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.institution_ids = set()
        institution_choices = [("", "Choose an institution")]
        for item in institutions:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            location = item.get("location") or {}
            place = ", ".join(
                str(location.get(key) or "").strip()
                for key in ("area", "state", "country")
                if location.get(key)
            )
            label = str(item.get("name") or "Institution")
            if item.get("campus"):
                label += f" — {item['campus']}"
            if place:
                label += f" · {place}"
            institution_choices.append((str(item["id"]), label))
            self.institution_ids.add(str(item["id"]))
        self.fields["institution_id"].choices = institution_choices

        location_rows = [
            item for item in locations if isinstance(item, dict) and item.get("id")
        ]
        self.location_ids = {str(item["id"]) for item in location_rows}
        area_choices = [("", "Choose a supported area")]
        for item in location_rows:
            area_choices.append(
                (
                    str(item["id"]),
                    ", ".join(
                        str(item.get(key) or "") for key in ("area", "state", "country")
                    ),
                )
            )
        self.fields["area_location_id"].choices = area_choices

        countries = sorted(
            {
                str(item.get("country") or "").strip()
                for item in location_rows
                if str(item.get("country") or "").strip()
            },
            key=str.casefold,
        )
        states = sorted(
            {
                str(item.get("state") or "").strip()
                for item in location_rows
                if str(item.get("state") or "").strip()
            },
            key=str.casefold,
        )
        self.state_country_pairs = {
            (
                str(item.get("country") or "").strip(),
                str(item.get("state") or "").strip(),
            )
            for item in location_rows
            if str(item.get("country") or "").strip()
            and str(item.get("state") or "").strip()
        }
        self.fields["state_country"].choices = [("", "Choose a country")] + [
            (value, value) for value in countries
        ]
        self.fields["state_name"].choices = [("", "Choose a state or region")] + [
            (value, value) for value in states
        ]

        service_choices = []
        self.service_ids_available = set()
        for item in offerings:
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            location = item.get("location") or {}
            place = (
                "Available anywhere"
                if not location
                else ", ".join(
                    str(location.get(key) or "")
                    for key in ("area", "state", "country")
                    if location.get(key)
                )
            )
            label = str(item.get("service_name") or "Service")
            if item.get("category_name"):
                label = f"{item['category_name']} · {label}"
            label += f" — {place}"
            service_choices.append((str(item["id"]), label))
            self.service_ids_available.add(str(item["id"]))
        self.fields["service_ids"].choices = service_choices

    def clean(self):
        cleaned = super().clean()
        promotion_type = cleaned.get("promotion_type")
        institution_id = cleaned.get("institution_id")
        area_location_id = cleaned.get("area_location_id")
        state_country = cleaned.get("state_country")
        state_name = cleaned.get("state_name")
        if promotion_type == "INSTITUTION":
            if institution_id not in self.institution_ids:
                self.add_error(
                    "institution_id",
                    "Select an institution from the current supported catalogue.",
                )
            if area_location_id or state_country or state_name:
                self.add_error(
                    "promotion_type",
                    "Institution Promotion accepts only an institution target.",
                )
        elif promotion_type == "AREA":
            if area_location_id not in self.location_ids:
                self.add_error(
                    "area_location_id", "Select an existing supported area or locality."
                )
            if institution_id or state_country or state_name:
                self.add_error(
                    "promotion_type",
                    "Area Promotion accepts only a supported area target.",
                )
        elif promotion_type == "STATE":
            if (state_country, state_name) not in self.state_country_pairs:
                self.add_error(
                    "state_name",
                    "Select a state that exists in the supported location catalogue for the selected country.",
                )
            if institution_id or area_location_id:
                self.add_error(
                    "promotion_type",
                    "State Promotion accepts only a country and state target.",
                )
        elif promotion_type == "HOMEPAGE":
            if institution_id or area_location_id or state_country or state_name:
                self.add_error(
                    "promotion_type",
                    "Homepage Feature does not use a geographical target.",
                )
        service_ids = cleaned.get("service_ids") or []
        if not service_ids:
            self.add_error(
                "service_ids", "Choose at least one active service offering."
            )
        elif any(value not in self.service_ids_available for value in service_ids):
            self.add_error(
                "service_ids",
                "Select only a service offering shown from your artisan profile.",
            )
        return cleaned


class PromotionDecisionForm(StyledForm):
    decision = forms.ChoiceField(
        choices=[
            ("APPROVED", "Approve this application"),
            ("CHANGES_REQUIRED", "Request changes"),
            ("REJECTED", "Reject this application"),
        ]
    )
    reason = forms.CharField(
        min_length=10,
        max_length=2000,
        label="Applicant-facing explanation",
        help_text="This explanation is included in the applicant notification. Do not enter internal-only notes here.",
        widget=forms.Textarea(attrs={"rows": 4, "maxlength": 2000}),
    )


class VerificationNINForm(StyledForm):
    nin = forms.RegexField(regex=r"^\d{11}$", label="NIN", min_length=11, max_length=11)
    consent = forms.BooleanField(
        label="I consent to this identity verification lookup."
    )


class VerificationBVNForm(StyledForm):
    bvn = forms.RegexField(regex=r"^\d{11}$", label="BVN", min_length=11, max_length=11)
    consent = forms.BooleanField(
        label="I consent to this identity verification lookup."
    )


class VerificationMethodForm(StyledForm):
    method = forms.CharField(max_length=100)


class OTPOnlyForm(StyledForm):
    otp = forms.RegexField(regex=r"^\d{6}$", min_length=6, max_length=6)


class ServiceRequestForm(StyledForm):
    """Customer service request.

    Deliberately short: catalogue (category/service), one location (country >
    state > area) and the few details an artisan actually needs. Time
    preferences, additional job details and a separate "service location"
    dropdown were removed as duplicate information.
    """

    # Rendered by the custom glass pickers; the value lives in a hidden input.
    category_name = forms.ChoiceField(
        label="Category",
        choices=[("", "Choose a category")],
        widget=forms.HiddenInput(),
    )
    service_name = forms.ChoiceField(
        label="Service",
        choices=[("", "Choose a category first")],
        widget=forms.HiddenInput(),
    )
    location_country_filter = forms.CharField(
        label="Country",
        max_length=100,
        required=False,
        widget=forms.HiddenInput(),
    )
    location_state_filter = forms.CharField(
        label="State or region",
        max_length=100,
        required=False,
        widget=forms.HiddenInput(),
    )
    location_area_filter = forms.CharField(
        label="Area or locality",
        max_length=150,
        required=False,
        widget=forms.HiddenInput(),
    )
    # Optional reference only: choosing an institution fills country/state/area;
    # it is never saved on its own.
    institution = forms.CharField(
        label="Closest institution (optional)",
        max_length=220,
        required=False,
        widget=forms.HiddenInput(),
    )
    # Resolved from country/state/area in clean(); never entered by the customer.
    location_id = forms.CharField(required=False, widget=forms.HiddenInput())
    preferred_date = forms.DateField(
        label="Preferred date",
        widget=forms.HiddenInput(attrs={"data-request-date-value": ""}),
    )
    urgency = forms.ChoiceField(
        label="Urgency",
        choices=[
            ("LOW", "Low"),
            ("NORMAL", "Normal"),
            ("HIGH", "High"),
            ("EMERGENCY", "Emergency"),
        ],
        initial="NORMAL",
        widget=forms.HiddenInput(),
    )
    budget = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=0,
        required=False,
        label="Budget",
        help_text="Optional. Leave blank if you are open to a quote.",
    )
    description = forms.CharField(
        label="Description",
        max_length=10000,
        widget=forms.Textarea(
            attrs={
                "rows": 5,
                "placeholder": "Describe the job, the problem and anything the artisan should bring.",
            }
        ),
    )

    def __init__(self, *args, categories=None, services=None, locations=None, **kwargs):
        super().__init__(*args, **kwargs)
        categories = categories or []
        services = services or []
        self.location_records = [
            {
                "id": str(item.get("id")),
                "country": str(item.get("country") or "").strip(),
                "state": str(item.get("state") or "").strip(),
                "area": str(item.get("area") or "").strip(),
            }
            for item in locations
            if isinstance(item, dict) and item.get("id")
        ]

        category_names = sorted(
            {
                str(item.get("name", "")).strip()
                for item in categories
                if isinstance(item, dict) and item.get("name")
            },
            key=str.casefold,
        )

        grouped_services = {}
        self.service_category_by_name = {}
        for item in services:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            category = str(item.get("category_name", "")).strip()
            if not name or not category:
                continue
            grouped_services.setdefault(category, []).append(name)
            self.service_category_by_name[name] = category

        for category in grouped_services:
            if category not in category_names:
                category_names.append(category)
        category_names.sort(key=str.casefold)

        self.fields["category_name"].choices = [("", "Choose a category")] + [
            (name, name) for name in category_names
        ]
        self.fields["service_name"].choices = [("", "Choose a category first")] + [
            (category, [(name, name) for name in sorted(names, key=str.casefold)])
            for category, names in sorted(
                grouped_services.items(), key=lambda pair: pair[0].casefold()
            )
        ]

    def clean(self):
        cleaned = super().clean()
        preferred_date = cleaned.get("preferred_date")
        if preferred_date and preferred_date < timezone.localdate():
            self.add_error("preferred_date", "Choose today or a later date.")
        category = cleaned.get("category_name")
        service = cleaned.get("service_name")
        actual_category = self.service_category_by_name.get(service)
        if service and category and actual_category != category:
            self.add_error(
                "service_name", "Choose a service from the selected category."
            )

        country = str(cleaned.get("location_country_filter") or "").strip()
        state = str(cleaned.get("location_state_filter") or "").strip()
        area = str(cleaned.get("location_area_filter") or "").strip()
        if country and state and area:
            match = next(
                (
                    record
                    for record in self.location_records
                    if record["country"].casefold() == country.casefold()
                    and record["state"].casefold() == state.casefold()
                    and record["area"].casefold() == area.casefold()
                ),
                None,
            )
            if match is None:
                from .geography import canonical_location

                worldwide = canonical_location(country, state, area)
                if worldwide is None:
                    self.add_error(
                        "location_area_filter",
                        "Choose a country, state and area from the geographic catalogue.",
                    )
                else:
                    cleaned["location_id"] = ""
                    for key, value in worldwide.items():
                        cleaned[f"location_{key}_filter"] = value
            else:
                cleaned["location_id"] = match["id"]
        elif str(cleaned.get("location_id") or "").strip() in {
            record["id"] for record in self.location_records
        }:
            # Callers that already post a catalogue id (older pages, API clients)
            # keep working; the pickers simply send country/state/area instead.
            cleaned["location_id"] = str(cleaned["location_id"]).strip()
        else:
            self.add_error(
                "location_area_filter",
                "Choose a country, state and area for this request.",
            )
        return cleaned


class ArtisanServiceForm(StyledForm):
    category_name = forms.ChoiceField(
        label="Category",
        choices=[("", "Select a category")],
        help_text="Choose a category to filter the service list.",
    )
    service_name = forms.ChoiceField(
        label="Service",
        choices=[("", "Choose a category first")],
    )
    location_country_filter = forms.CharField(
        label="Country",
        max_length=100,
        required=False,
        widget=forms.TextInput(
            attrs={
                "data-geography-country": "catalogue-location",
                "data-geography-default": "NG",
                "data-location-country-filter": "",
            }
        ),
    )
    location_state_filter = forms.CharField(
        label="State or region",
        max_length=100,
        required=False,
        widget=forms.TextInput(
            attrs={
                "data-geography-state": "catalogue-location",
                "data-location-state-filter": "",
            }
        ),
    )
    location_area_filter = forms.CharField(
        label="Area or locality",
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={"data-location-area-filter": ""}),
    )
    institution = forms.CharField(
        label="Nearby institution (optional)",
        max_length=200,
        required=False,
        initial=None,
        widget=forms.TextInput(
            attrs={"placeholder": "Optional reference, e.g. FUNAAB"}
        ),
        help_text="Optional reference only. Choose a registered marketplace location below; no institution or new location is saved.",
    )
    location_id = forms.CharField(
        label="Service coverage",
        required=False,
        max_length=32,
        help_text=(
            "Anywhere uses the API's general-offering contract. Otherwise pick a "
            "country, state and area from the geographic catalogue."
        ),
    )
    starting_price = forms.DecimalField(
        label="Starting budget",
        max_digits=12,
        decimal_places=2,
        min_value=0,
        required=False,
        help_text="Optional starting budget for this service offering.",
    )

    ANYWHERE_COVERAGE_VALUE = "__anywhere__"
    GEOGRAPHIC_COVERAGE_VALUE = "__geo__"

    def __init__(self, *args, categories=None, services=None, locations=None, **kwargs):
        super().__init__(*args, **kwargs)
        categories = categories or []
        services = services or []
        # Location choices come from the client-side geographic catalogue; the
        # hidden coverage field only ever carries a sentinel value.
        self.fields["location_id"].widget.attrs["data-location-anywhere-value"] = (
            self.ANYWHERE_COVERAGE_VALUE
        )
        if not self.is_bound:
            if not str(self.initial.get("location_id") or "").strip():
                self.initial["location_id"] = self.ANYWHERE_COVERAGE_VALUE
            self.initial.setdefault("location_country_filter", "Nigeria")

        category_names = sorted(
            {
                str(item.get("name", "")).strip()
                for item in categories
                if isinstance(item, dict) and item.get("name")
            },
            key=str.casefold,
        )
        service_groups = {}
        self.service_category_by_name = {}
        for item in services:
            if not isinstance(item, dict):
                continue
            service_name = str(item.get("name", "")).strip()
            category_name = str(item.get("category_name", "")).strip()
            if not service_name or not category_name:
                continue
            service_groups.setdefault(category_name, []).append(service_name)
            self.service_category_by_name[service_name] = category_name

        # The category is a UI filter only. The API derives it from service_name.
        for category_name in service_groups:
            if category_name not in category_names:
                category_names.append(category_name)
        category_names.sort(key=str.casefold)
        self.fields["category_name"].choices = [("", "Select a category")] + [
            (name, name) for name in category_names
        ]
        self.fields["service_name"].choices = [("", "Choose a category first")] + [
            (category_name, [(name, name) for name in sorted(names, key=str.casefold)])
            for category_name, names in sorted(
                service_groups.items(), key=lambda pair: pair[0].casefold()
            )
        ]

        self._service_category_by_name = self.service_category_by_name

    def clean(self):
        cleaned = super().clean()
        service_name = cleaned.get("service_name")
        category_name = cleaned.get("category_name")
        service_category = self._service_category_by_name.get(service_name)
        if service_name and category_name and service_category != category_name:
            self.add_error(
                "service_name", "Choose a service from the selected category."
            )

        coverage = str(cleaned.get("location_id") or "").strip()
        if coverage == self.ANYWHERE_COVERAGE_VALUE:
            cleaned["resolved_location"] = None
        elif coverage == self.GEOGRAPHIC_COVERAGE_VALUE:
            canonical = canonical_location(
                cleaned.get("location_country_filter"),
                cleaned.get("location_state_filter"),
                cleaned.get("location_area_filter"),
            )
            if canonical is None:
                raise forms.ValidationError(
                    "Choose coverage as Anywhere, or pick a country, state and "
                    "area from the geographic catalogue."
                )
            cleaned["location_country_filter"] = canonical["country"]
            cleaned["location_state_filter"] = canonical["state"]
            cleaned["location_area_filter"] = canonical["area"]
            cleaned["resolved_location"] = canonical
        else:
            raise forms.ValidationError(
                "Choose coverage as Anywhere, or pick a country, state and area."
            )
        return cleaned


class BookingForm(StyledForm):
    request_id = forms.IntegerField(label="Service request ID")
    artisan_id = forms.UUIDField(label="Artisan")
    scheduled_time_start = forms.DateTimeField(
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"})
    )
    scheduled_time_end = forms.DateTimeField(
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"})
    )
    price_agreed = forms.DecimalField(max_digits=12, decimal_places=2, min_value=0.01)


class ReasonForm(StyledForm):
    reason = forms.CharField(max_length=1000, widget=forms.Textarea(attrs={"rows": 4}))


class DisputeMessageForm(StyledForm):
    message = forms.CharField(
        label="Message to customer support",
        min_length=2,
        max_length=1000,
        strip=True,
        widget=forms.Textarea(
            attrs={
                "rows": 6,
                "placeholder": "Explain what happened and what you need customer support to review.",
                "autocomplete": "off",
                "data-min-words": "6",
                "data-support-description": "",
            }
        ),
        help_text="Use at least six meaningful words. This message will be added to the dispute case and sent to customer support. Do not include passwords, payment PINs, or one-time codes.",
    )

    def clean_message(self):
        return _normalize_support_description(self.cleaned_data["message"])


class CancelForm(StyledForm):
    reason_code = forms.ChoiceField(
        choices=[
            ("CUSTOMER_CHANGED_MIND", "Changed plans"),
            ("SCHEDULE_CONFLICT", "Scheduling conflict"),
            ("FOUND_ALTERNATIVE", "Found another artisan"),
            ("PRICE_DISAGREEMENT", "Price or terms disagreement"),
            ("DUPLICATE_BOOKING", "Duplicate booking"),
            ("PAYMENT_FAILURE", "Payment failure"),
            ("ARTISAN_UNRESPONSIVE", "Artisan unresponsive"),
            ("ARTISAN_CANCELLED", "Artisan cancelled"),
            ("ARTISAN_LATE", "Artisan late"),
            ("SERVICE_NOT_AS_DESCRIBED", "Service not as described"),
            ("SAFETY_CONCERN", "Safety concern"),
            ("EMERGENCY", "Emergency"),
            ("OTHER", "Other"),
        ]
    )
    reason = forms.CharField(max_length=1000, widget=forms.Textarea(attrs={"rows": 4}))


class NoteForm(StyledForm):
    note = forms.CharField(
        max_length=1000, required=False, widget=forms.Textarea(attrs={"rows": 4})
    )


class ReviewForm(StyledForm):
    booking = forms.UUIDField(label="Booking")
    rating = forms.IntegerField(min_value=1, max_value=5)
    comment = forms.CharField(
        max_length=2000, required=False, widget=forms.Textarea(attrs={"rows": 4})
    )


class LocationForm(StyledForm):
    country = forms.CharField(
        max_length=100,
        widget=forms.TextInput(
            attrs={
                "data-geography-country": "location-form",
                "data-geography-default": "NG",
            }
        ),
    )
    state = forms.CharField(
        max_length=100,
        widget=forms.TextInput(attrs={"data-geography-state": "location-form"}),
    )
    area = forms.CharField(
        max_length=150,
        help_text="Use the existing area name; this catalogue does not invent locality options.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not str(self.initial.get("country") or "").strip():
            self.initial["country"] = "Nigeria"


class SupportCaseForm(StyledForm):
    conversation_id = forms.UUIDField(required=False, widget=forms.HiddenInput())
    promotion_campaign = forms.UUIDField(
        required=False,
        label="Promotion campaign ID (optional)",
        help_text="For a promotion complaint or payment issue, use the campaign ID from My Promotions or follow its support link. A campaign case cannot also reference a conversation.",
    )
    case_type = forms.ChoiceField(
        choices=[
            ("REPORT", "Report"),
            ("PAYMENT", "Payment"),
            ("ACCOUNT", "Account"),
            ("TECHNICAL", "Technical"),
            ("OTHER", "Other"),
        ]
    )
    priority = forms.ChoiceField(
        choices=[
            ("LOW", "Low"),
            ("MEDIUM", "Medium"),
            ("HIGH", "High"),
            ("URGENT", "Urgent"),
        ]
    )
    subject = forms.CharField(max_length=200)
    description = forms.CharField(
        max_length=10000,
        strip=True,
        widget=forms.Textarea(
            attrs={"rows": 5, "data-min-words": "6", "data-support-description": ""}
        ),
        help_text="Enter at least six meaningful words. A booking or payment case should start from its relevant conversation so the correct transaction is attached automatically.",
    )

    def clean_description(self):
        return _normalize_support_description(self.cleaned_data["description"])

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("conversation_id") and cleaned.get("promotion_campaign"):
            raise forms.ValidationError(
                "A support case can reference a conversation or a promotion campaign, not both."
            )
        if cleaned.get("case_type") == "PAYMENT" and not (
            cleaned.get("conversation_id") or cleaned.get("promotion_campaign")
        ):
            raise forms.ValidationError(
                "For a payment case, open the relevant conversation or promotion summary first so its transaction can be linked."
            )
        return cleaned


class SupportCaseMessageForm(StyledForm):
    message = forms.CharField(
        min_length=2,
        max_length=5000,
        strip=True,
        widget=forms.Textarea(
            attrs={
                "rows": 4,
                "placeholder": "Share an update or ask customer support a question…",
                "aria-describedby": "case-message-help case-message-count",
            }
        ),
    )


class AssignForm(StyledForm):
    staff_user_id = forms.ChoiceField(
        choices=[("", "Choose a support teammate")],
        label="Assign to",
    )

    def __init__(self, *args, staff=None, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [("", "Choose a support teammate")]
        for member in staff or []:
            name = (
                member.get("name")
                or member.get("email")
                or f"Staff #{member.get('id')}"
            )
            email = member.get("email")
            label = f"{name} · {email}" if email and email != name else name
            choices.append((str(member.get("id")), label))
        self.fields["staff_user_id"].choices = choices


class ResolveForm(StyledForm):
    resolution_code = forms.ChoiceField(
        choices=[
            ("NO_ADJUSTMENT", "No financial adjustment"),
            ("REBOOK", "Rebook"),
            ("REPERFORM", "Re-perform service"),
            ("POLICY_ACTION", "Policy action"),
            ("OTHER", "Other"),
            ("REFUND", "Refund"),
            ("PARTIAL_REFUND", "Partial refund"),
        ],
        help_text="A refund must first finish through the verified finance workflow. Payout outcomes cannot be recorded as a case-only resolution.",
    )
    resolution_note = forms.CharField(
        min_length=5, widget=forms.Textarea(attrs={"rows": 4})
    )


class RefundRequestForm(StyledForm):
    payment_id = forms.ChoiceField(
        choices=[("", "No eligible payments found")],
        label="Payment",
    )
    amount = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=100,
        label="Refund amount (NGN)",
        help_text="Minimum ₦100. Whole-naira amounts only; the eligible balance is checked again by the API.",
        widget=forms.NumberInput(
            attrs={
                "min": "100",
                "step": "1",
                "inputmode": "decimal",
                "data-support-refund-amount": "",
            }
        ),
    )
    reason = forms.CharField(
        min_length=5,
        max_length=2000,
        label="Reason for refund",
        widget=forms.Textarea(attrs={"rows": 4}),
    )

    def __init__(self, *args, eligible_payments=None, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [("", "Choose an eligible payment")]
        for payment in eligible_payments or []:
            remaining = payment.get("remaining_amount", 0)
            try:
                display_amount = f"₦{float(remaining):,.2f} remaining"
            except (TypeError, ValueError):
                display_amount = "Refundable balance available"
            label = f"{payment.get('reference', 'Payment')} · {payment.get('status', '').replace('_', ' ').title()} · {display_amount}"
            choices.append((str(payment.get("id")), label))
        self.fields["payment_id"].choices = choices
        self.fields["payment_id"].widget.attrs["data-support-payment-select"] = ""
        if not eligible_payments:
            self.fields["payment_id"].help_text = (
                "No verified, refundable payment is available for this case."
            )

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount != amount.to_integral_value():
            raise forms.ValidationError(
                "Refunds must be requested in whole-naira amounts."
            )
        return amount


class ApprovalForm(StyledForm):
    approval_note = forms.CharField(
        min_length=5, widget=forms.Textarea(attrs={"rows": 4})
    )


class RejectionForm(StyledForm):
    rejection_note = forms.CharField(
        min_length=5, widget=forms.Textarea(attrs={"rows": 4})
    )


class RolesForm(StyledForm):
    roles = forms.MultipleChoiceField(
        choices=[
            ("CUSTOMER_SUPPORT_AGENT", "Support Agent"),
            ("CUSTOMER_SUPPORT_SUPERVISOR", "Support Supervisor"),
            ("CUSTOMER_SUPPORT_FINANCE", "Support Finance"),
            ("CUSTOMER_SUPPORT_ADMIN", "Support Admin"),
        ],
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )


class PayoutAccountForm(StyledForm):
    account_name = forms.CharField(
        max_length=150,
        label="Account Name",
        help_text="Enter the account holder name shown by the bank. It must match your verified identity and profile.",
    )
    bank_code = forms.ChoiceField(
        choices=[("", "Choose a supported bank")],
        label="Bank",
        widget=forms.Select(attrs={"data-payout-bank-select": ""}),
    )
    account_number = forms.RegexField(
        regex=r"^\d{10}$",
        max_length=10,
        min_length=10,
        label="Account Number",
        help_text="Enter the 10-digit Nigerian account number. It is never displayed in full.",
        widget=forms.PasswordInput(
            attrs={
                "autocomplete": "off",
                "inputmode": "numeric",
                "maxlength": "10",
                "pattern": "[0-9]{10}",
            }
        ),
    )
    otp = forms.RegexField(
        regex=r"^\d{6}$",
        max_length=6,
        min_length=6,
        required=False,
        label="Security code",
        help_text="Enter the 6-digit code emailed to you to approve this payout account change.",
        widget=forms.TextInput(
            attrs={
                "autocomplete": "one-time-code",
                "inputmode": "numeric",
                "maxlength": "6",
                "pattern": "[0-9]{6}",
            }
        ),
    )

    def __init__(self, *args, banks=None, locked=False, requires_otp=False, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [("", "Choose a supported bank")]
        for bank in banks or []:
            if not isinstance(bank, dict):
                continue
            code = str(bank.get("code") or "").strip()
            name = str(bank.get("name") or "").strip()
            if code and name:
                choices.append((code, name))
        self.fields["bank_code"].choices = choices
        self.fields["bank_code"].widget.attrs["data-payout-bank-select"] = ""
        if locked:
            # Saved payout details stay read-only until the artisan starts a
            # verified change request.
            for field in self.fields.values():
                field.disabled = True
        if requires_otp:
            self.fields["otp"].required = True
        else:
            self.fields.pop("otp", None)
