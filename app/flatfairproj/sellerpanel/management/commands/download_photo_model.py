from django.core.management.base import BaseCommand
from pretrained import TRAINING, verifyModel
from sellerpanel.photocheck import CLIP_NAME, CLIP_REVISION, ML

NAME = CLIP_NAME
REVISION = CLIP_REVISION

FILES = [
    "config.json",
    "preprocessor_config.json",
    "processor_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.json",
    "merges.txt",
    "special_tokens_map.json",
    "pytorch_model.bin",
]


def clip_ready():
    return (
        (ML / "config.json").is_file()
        and any(
            (ML / file_name).is_file() for file_name in ["model.safetensors", "pytorch_model.bin"]
        )
        and any(
            (ML / file_name).is_file()
            for file_name in ["preprocessor_config.json", "processor_config.json"]
        )
        and (
            (ML / "tokenizer.json").is_file()
            or all((ML / file_name).is_file() for file_name in ["vocab.json", "merges.txt"])
        )
    )


class Command(BaseCommand):
    help = "Install the missing CLIP photo-check weights in assets/ml/clip."

    def handle(self, *args, **options):
        if clip_ready():
            self.stdout.write(f"CLIP already installed: {ML} ({REVISION})")
        else:
            import torch



            from huggingface_hub import snapshot_download


            from transformers import CLIPModel, CLIPProcessor



            torch.set_num_threads(4)
            self.stdout.write(f"Downloading {NAME} (about 600 MB)...")


            folder = snapshot_download(NAME, revision=REVISION, allow_patterns=FILES)



            CLIPModel.from_pretrained(folder, local_files_only=True).save_pretrained(ML)



            CLIPProcessor.from_pretrained(folder, local_files_only=True).save_pretrained(ML)


        status = verifyModel(NAME, ML, REVISION)



        nextUp = ""


        if status == "not installed":
            nextUp = "; run python manage.py download_photo_model to install the missing files"
        elif status != "verified":
            script = TRAINING / "write_manifest.py"


            nextUp = f"; after reviewing the new files, run python {script}"



        self.stdout.write(f"CLIP: {status}{nextUp}")



        self.stdout.write(f'Photo model: CLIP {"on" if status == "verified" else "off"}.')
