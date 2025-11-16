from setuptools import setup, find_packages

setup(
    name="openmux",
    version="1.0.0",
    packages=find_packages(),
    install_requires=[
        "pyserial>=3.5",
        "PyYAML>=6.0",
        "cryptography>=38.0.0",
    ],
    extras_require={
        "web": ["fastapi>=0.68.0", "uvicorn>=0.15.0"],
    },
    entry_points={
        "console_scripts": [
            "openmux-server=openmux.server.main:main",
            "openmux-client=openmux.client.main:main",
        ],
    },
    author="OpenMux Team",
    author_email="info@openmux.org",
    description="Serial port multiplexing server and client",
    keywords="serial, console, server, client",
    python_requires=">=3.9",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Intended Audience :: System Administrators",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "License :: OSI Approved :: MIT License",
        "Topic :: System :: Networking",
        "Topic :: Terminals :: Serial",
    ],
)
