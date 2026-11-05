from django import forms

from .forms import StyledForm


class SocialProfileForm(StyledForm):
    """Collect only names missing from an already-verified provider identity."""

    first_name = forms.CharField(
        max_length=40,
        widget=forms.TextInput(attrs={"autocomplete": "given-name"}),
    )
    last_name = forms.CharField(
        max_length=40,
        widget=forms.TextInput(attrs={"autocomplete": "family-name"}),
    )
