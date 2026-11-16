from setuptools import setup, find_packages

setup(
    name="agent-mesh",
    version="2.2.1",
    packages=find_packages(),
    package_data={"agent_mesh": ["handover_rules.md", "web2/*",
                                 "web/*"]},
    entry_points={
        "console_scripts": [
            "agent-mesh=agent_mesh.cli:main",
        ],
    },
)
