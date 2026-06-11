"""
Setup file — allows the QA agent to be installed as a package via pip.
pip install git+https://github.com/Timefrugal/Timefrugal-QA.git@main
"""
from setuptools import setup, find_packages

setup(
    name="timefrugal-qa",
    version="1.0.0",
    description="AI-powered QA agent for Python repos — free via GitHub Models",
    author="Timefrugal",
    packages=find_packages(),
    python_requires=">=3.10",
    install_requires=[
        "openai>=1.30.0",
        "requests>=2.31.0",
        "rich>=13.7.0",
    ],
    entry_points={
        "console_scripts": [
            "timefrugal-qa=qa_agent.__main__:main",
        ],
    },
)
