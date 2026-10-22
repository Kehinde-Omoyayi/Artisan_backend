from django.http import FileResponse, HttpResponse
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import NotFound
from rest_framework.throttling import ScopedRateThrottle
from .permissions import IsConversationParticipant
from .services import get_conversation_for_user
from .models import ConversationAttachment
from .private_media import attachment_storage
from .retention import access_blocked


class ConversationMediaView(APIView):
    permission_classes = [IsAuthenticated, IsConversationParticipant]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "media_upload"

    def get(self, request, conversation_id, attachment_id):
        conversation = get_conversation_for_user(
            user=request.user, conversation_id=conversation_id
        )
        item = (
            ConversationAttachment.objects.select_related("message__conversation")
            .filter(
                pk=attachment_id,
                message__conversation=conversation,
                scan_status="CLEAN",
            )
            .first()
        )
        if not item or access_blocked(item):
            raise NotFound("Media is not available.")
        store = attachment_storage(item)
        try:
            handle = store.open(item.file.name, "rb")
        except FileNotFoundError:
            raise NotFound("Media is not available.") from None
        from .byte_ranges import bounds, LimitedFile

        size = store.size(item.file.name)
        try:
            part = bounds(request.headers.get("Range", ""), size)
        except ValueError:
            handle.close()
            response = HttpResponse(status=416)
            response["Content-Range"] = f"bytes */{size}"
        else:
            if part:
                start, end = part
                handle.seek(start)
                response = FileResponse(
                    LimitedFile(handle, end - start + 1),
                    status=206,
                    content_type=item.content_type,
                    filename="service-media",
                )
                response["Content-Range"] = f"bytes {start}-{end}/{size}"
                response["Content-Length"] = str(end - start + 1)
            else:
                response = FileResponse(
                    handle,
                    content_type=item.content_type,
                    as_attachment=False,
                    filename="service-media",
                )
        response["Accept-Ranges"] = "bytes"
        response["Cache-Control"] = "private, no-store, max-age=0"
        response["X-Content-Type-Options"] = "nosniff"
        response["Referrer-Policy"] = "no-referrer"
        response["Content-Security-Policy"] = "default-src 'none'; sandbox"
        return response
