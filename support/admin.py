from django.contrib import admin

from .models import (
    SupportCase,
    SupportCaseEvent,
    SupportFinancialAction,
)


admin.site.register(SupportCase)
admin.site.register(SupportCaseEvent)
admin.site.register(SupportFinancialAction)