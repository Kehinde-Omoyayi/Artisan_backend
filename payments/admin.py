from django.contrib import admin

from .models import *

admin.site.register(EscrowPayment)
admin.site.register(EscrowRelease)
admin.site.register(PaymentWebhookEvent)
admin.site.register(ArtisanPayoutAccount)
