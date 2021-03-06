from django.contrib import admin

from .models import EscrowKeypair


class EscrowKeypairAdmin(admin.ModelAdmin):

    list_display = (
        "created_at",
        "keychain_uid",
        "key_type",
        "decryption_authorized_at",
    )

    ordering = ["-id"]

    readonly_fields = ("created_at",)


admin.site.register(EscrowKeypair, EscrowKeypairAdmin)
