from io import BytesIO
from pathlib import Path
from zipfile import ZipFile
from PIL import Image
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from storefront.models import needEmail, lock_user, Listing
from storefront.notices import notify
from .access import canOpen
from .models import DocumentFile, DocumentRequest

MAX_DOCUMENT_SIZE = 10 * 1024 * 1024

DOCUMENT_TYPES = {
    ".pdf": ("application/pdf", b"%PDF-", None),
    ".jpg": ("image/jpeg", b"\xff\xd8\xff", "JPEG"),
    ".jpeg": ("image/jpeg", b"\xff\xd8\xff", "JPEG"),
    ".png": ("image/png", b"\x89PNG\r\n\x1a\n", "PNG"),
}


def check_docs(uploads):
    if not 1 <= len(uploads) <= 5:
        raise ValidationError("Choose between 1 and 5 files.")

    for upload in uploads:
        checkFile(upload)


def checkFile(upload, types=DOCUMENT_TYPES, maxSize=MAX_DOCUMENT_SIZE):
    ending = Path(upload.name).suffix.lower()
    wanted = types.get(ending)

    if not wanted or upload.content_type != wanted[0]:
        raise ValidationError("Upload files with the matching file type.")



    if upload.size > maxSize:
        raise ValidationError(
            f"Each file must be {maxSize // (1024 * 1024)} MB or smaller."
        )

    data = upload.read(maxSize + 1)


    upload.seek(0)

    if len(data) > maxSize:
        raise ValidationError(
            f"Each file must be {maxSize // (1024 * 1024)} MB or smaller."
        )

    if not data.startswith(wanted[1]):
        raise ValidationError("A file does not match its declared format.")

    if wanted[2]:
        try:
            with Image.open(BytesIO(data)) as image:
                if image.format != wanted[2]:
                    raise ValueError

                image.verify()
        except Exception as error:
            raise ValidationError("A file does not match its JPG or PNG format.")

    if ending == ".zip":
        try:
            with ZipFile(BytesIO(data)) as archive:
                archive.infolist()
        except Exception as error:
            raise ValidationError("A file does not match its ZIP format.")


@login_required
@require_POST
@transaction.atomic
@needEmail
def sendDocs(request, pk):
    from .views import audit


    docAsk = get_object_or_404(DocumentRequest, pk=pk, listing__seller=request.user)

    lock_user(request.user.pk)

    flat = get_object_or_404(
        Listing.objects.select_for_update(),
        pk=docAsk.listing_id,
        seller=request.user,
        status="review",
    )


    docAsk = get_object_or_404(DocumentRequest.objects.select_for_update(), pk=pk)

    latest = flat.documentRequests.order_by("-createdAt", "-pk").first()



    if latest.pk != pk or docAsk.status not in ("requested", "rejected"):
        raise Http404

    url = reverse("my_listings") + f"#listing-{flat.publicId}"
    uploads = request.FILES.getlist("documents")


    try:
        check_docs(uploads)
    except ValidationError as error:
        messages.error(request, error.messages[0])

        return redirect(url)



    oldOnes = list(docAsk.files.all())
    saved = []

    try:
        for upload in uploads:
            doc = DocumentFile(
                request=docAsk,
                originalName=upload.name,
                size=upload.size,
                contentType=DOCUMENT_TYPES[Path(upload.name).suffix.lower()][0],
            )



            doc.file.save(upload.name, upload, save=False)



            saved.append(doc.file)
            doc.save()

        docAsk.files.filter(pk__in=[document.pk for document in oldOnes]).delete()

        docAsk.status = "submitted"
        docAsk.submittedAt = timezone.now()
        docAsk.updatedAt = docAsk.submittedAt
        docAsk.reviewedAt = None
        docAsk.staffReason = ""
        docAsk.save()
        teamUrl = reverse("admin_queue") + f"#queue-{flat.pk}"


        for staff in User.objects.filter(is_active=True, is_staff=True).distinct():
            notify(
                staff,
                "listing_documents",
                f"Documents submitted for listing #{flat.pk}. Please review them.",
                teamUrl,
            )

        audit(
            request,
            "documents submitted",
            f"Listing #{flat.pk}",
            f"{len(uploads)} files",
        )
    except Exception:
        for file in saved:
            file.storage.delete(file.name)

        raise

    for doc in oldOnes:
        transaction.on_commit(lambda file=doc.file: file.storage.delete(file.name))

    messages.success(request, "Documents submitted. Our team will check them.")

    return redirect(url)


@login_required
def getDoc(request, pk):
    from .views import audit

    doc = get_object_or_404(
        DocumentFile.objects.select_related("request__listing"), pk=pk
    )

    if doc.request.listing.seller_id != request.user.pk and not canOpen(
        request.user, "admin_queue"
    ):
        raise Http404

    try:
        reply = FileResponse(
            doc.file.open("rb"),
            as_attachment=True,
            filename=doc.originalName,
            content_type=doc.contentType,
        )
    except Exception as error:
        raise Http404

    reply["Cache-Control"] = "private, no-store"
    reply["X-Content-Type-Options"] = "nosniff"


    audit(
        request,
        "document downloaded",
        f"Document #{doc.pk} (Listing #{doc.request.listing_id})",
    )

    return reply
