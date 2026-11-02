from django.core.management.base import BaseCommand


from pretrained import TRAINING, verifyModel, MODELS, REVISIONS as PIPELINE_REVISIONS


from vision.pipeline import ML


REVISIONS = {repo: PIPELINE_REVISIONS[repo] for repo, directory in MODELS}


class Command(BaseCommand):
    help = "Download Grounding DINO, SlimSAM, Florence-2 and Depth Anything V2 weights (requires internet)."

    def handle(self, *args, **options):
        from huggingface_hub import snapshot_download

        for repo, folder in MODELS:
            target = ML / folder

            if (
                (target / "model.safetensors").exists()
                and (target / "config.json").exists()
                and (target / "preprocessor_config.json").exists()
            ):
                self.stdout.write(f"{folder}: already downloaded ({REVISIONS[repo]})")
            else:
                snapshot_download(repo_id=repo, revision=REVISIONS[repo], local_dir=str(target))
                self.stdout.write(f"{folder}: downloaded")

            status = verifyModel(repo, target, REVISIONS[repo])
            nextUp = ""

            if status == "not installed":
                nextUp = (
                    "; run python manage.py download_vision_models to install the missing files"
                )
            elif status != "verified":
                script = TRAINING / "write_manifest.py"
                nextUp = f"; after reviewing the new files, run python {script}"

            self.stdout.write(f"{folder}: {status}{nextUp}")
