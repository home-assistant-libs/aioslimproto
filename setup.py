"""Setup for aioslimproto."""
from pathlib import Path

from setuptools import find_packages, setup

PROJECT_DIR = Path(__file__).parent.resolve()
README_FILE = PROJECT_DIR / "README.rst"
REQUIREMENTS_FILE = PROJECT_DIR / "requirements.txt"
PACKAGES = find_packages(exclude=["tests", "tests.*"])
PROJECT_REQ_PYTHON_VERSION = "3.9"

setup(
    name="aioslimproto",
    version="0.1.0",
    license="Apache License 2.0",
    url="https://github.com/music-assistant/aioslimproto",
    author="Marcel van der Veldt",
    author_email="marcelveldt@users.noreply.github.com",
    description="Python module to talk to Logitech Squeezebox players directly (without Logitech server).",
    long_description=README_FILE.read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    packages=PACKAGES,
    zip_safe=True,
    platforms="any",
    install_requires=REQUIREMENTS_FILE.read_text(encoding="utf-8"),
    python_requires=f">={PROJECT_REQ_PYTHON_VERSION}",
    classifiers=[
        "Intended Audience :: Developers",
        "Operating System :: OS Independent",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
)
