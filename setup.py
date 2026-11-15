from setuptools import setup, find_packages

setup(
    name="agent-mesh",
    version="2.0.1",
    packages=find_packages(),
    entry_points={
        "console_scripts": [
            "agent-mesh=agent_mesh.cli:main",
        ],
    },
)
