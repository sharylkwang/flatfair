from unittest.mock import MagicMock, patch

from django.core.files.storage import default_storage
from django.utils import timezone

from sellerpanel import photos
from sellerpanel.photoqueue import queuePics
from storefront.models import PhotoCheckJob

CARD = {"name": "Tan Wei Ling", "number": "4242 4242 4242 4242", "expiry": "12/30", "cvc": "123"}


def stripe_reply(data):
    reply = MagicMock(status_code=200)
    reply.json.return_value = data
    return reply


class CompletedPhotoQueueMixin:

    def _callSetUp(self):

        def complete(owner, paths, previousPaths=None):
            images = []
            for path in paths.values():
                with default_storage.open(path, "rb") as image:
                    images.append(image.read())
            results = photos.checkPics(images)
            jobs = queuePics(owner, paths, previousPaths=previousPaths)
            for job, result in zip(jobs.values(), results):
                PhotoCheckJob.objects.filter(pk=job.pk).update(
                    status="done",
                    result=result or {"unchecked": True},
                    finishedAt=timezone.now(),
                    attempts=1,
                )
            return jobs

        for target, replacement in [
            ("sellerpanel.photoqueue.ensureWorker", None),
            ("sellerpanel.photos.ensureWorker", None),
            ("sellerpanel.photos.queuePics", complete),
        ]:
            mocked = patch(target, side_effect=replacement)
            mocked.start()
            self.addCleanup(mocked.stop)
        super()._callSetUp()
