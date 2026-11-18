from django import forms


class SocialProfileForm(forms.Form):
    first_name = forms.CharField(max_length=40, label="First name", strip=True)
    last_name = forms.CharField(max_length=40, label="Last name", strip=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.widget.attrs.update({
                "class": "field-input",
                "autocomplete": "given-name" if name == "first_name" else "family-name",
            })
