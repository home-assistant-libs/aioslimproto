"""Setup for aioslimproto."""
from setuptools import find_packages, setup

LONG_DESC = open("README.md").read()
PACKAGES = find_packages(exclude=["tests", "tests.*"])
REQUIREMENTS = list(val.strip() for val in open("requirements.txt"))
MIN_PY_VERSION = "3.9"

setup(
    name="aioslimproto",
    version="0.1.0",
    license="Apache License 2.0",
    url="https://github.com/music-assistant/aioslimproto",
    author="Marcel van der Veldt",
    author_email="marcelveldt@users.noreply.github.com",
    description="Python module to talk to Logitech Squeezebox players directly (without Logitech server).",
    long_description=LONG_DESC,
    long_description_content_type="text/markdown",
    packages=PACKAGES,
    zip_safe=True,
    platforms="any",
    install_requires=REQUIREMENTS,
    python_requires=f">={MIN_PY_VERSION}",
    classifiers=[
        "Intended Audience :: Developers",
        "Operating System :: OS Independent",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
)
