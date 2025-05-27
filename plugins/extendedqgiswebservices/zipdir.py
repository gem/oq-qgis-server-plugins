import zipfile
import os


def _zipdir_worker(path, ziph):
    # ziph is zipfile handle
    for root, dirs, files in os.walk(path):
        for file in files:
            filepath = os.path.join(root, file)
            ziph.write(filepath)


def zipdir(file_path, arch_path):
    with zipfile.ZipFile(file_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        _zipdir_worker(arch_path, zipf)
