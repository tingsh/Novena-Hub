"""Resend controls for allauth's email verification code page."""

import time

from allauth.account import app_settings
from allauth.account.internal.flows.email_verification import (
    consume_email_verification_rate_limit,
    send_verification_email,
)
from allauth.account.views import ConfirmEmailVerificationCodeView, email_verification_sent
from allauth.core.exceptions import ImmediateHttpResponse
from django.contrib import messages
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache

RESEND_COOLDOWN_SECONDS = 30
MAX_RESENDS = 2


@method_decorator(never_cache, name="dispatch")
class ResendableEmailVerificationCodeView(ConfirmEmailVerificationCodeView):
    def dispatch(self, request, *args, **kwargs):
        if not app_settings.EMAIL_VERIFICATION_BY_CODE_ENABLED:
            return email_verification_sent(request)
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["resend_limit_reached"] = self._process.state.get("resend_count", 0) >= MAX_RESENDS
        return context

    def post(self, request, *args, **kwargs):
        if request.POST.get("action") == "restart":
            self._process.abort()
            return HttpResponseRedirect(reverse("account_login"))
        if request.POST.get("action") != "resend":
            return super().post(request, *args, **kwargs)

        process = self._process
        user = process.user
        email = process.state.get("email")
        # A verification session must belong to the pending login (or the
        # authenticated account when confirming an additional address).
        authorized = (
            user
            and email
            and (
                (self.stage and self.stage.login.user.pk == user.pk)
                or (request.user.is_authenticated and request.user.pk == user.pk)
            )
        )
        if not authorized or process.email_address.verified:
            return HttpResponseRedirect(reverse("account_login"))

        count = process.state.get("resend_count", 0)
        if count >= MAX_RESENDS:
            messages.error(request, "Resend limit reached. Sign in again to request a new code.")
        elif time.time() - process.state["at"] < RESEND_COOLDOWN_SECONDS:
            messages.info(request, "Please wait 30 seconds before requesting another code.")
        elif not consume_email_verification_rate_limit(request, email, dry_run=True):
            messages.info(request, "Please wait a moment before requesting another code.")
        else:
            try:
                sent = send_verification_email(request, user, email=email)
            except ImmediateHttpResponse:
                sent = False
            if sent:
                # Sending creates a fresh process. Retain the resend limit in it.
                fresh = request.session["account_email_verification_code"]
                fresh["resend_count"] = count + 1
                request.session.modified = True
                messages.success(request, "A new verification code is on its way. Use the latest email.")
            else:
                messages.error(request, "We could not send a new code. Please try again shortly.")
        return HttpResponseRedirect(reverse("account_email_verification_sent"))
