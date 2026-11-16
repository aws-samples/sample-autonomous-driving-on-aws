"""Every stack and physical resource name derives from one optional stage, so several copies can share an account."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Naming:
    stage: str = ""

    def __post_init__(self):
        if self.stage and not re.fullmatch(r"[a-z][a-z0-9]{0,11}", self.stage):
            raise ValueError("drive-atlas:stage must be 1-12 lowercase letters/digits, starting with a letter")

    @property
    def prefix(self) -> str:
        """drive-atlas or drive-atlas-<stage>: ECR repositories, cluster, Neptune."""
        return "drive-atlas" + (f"-{self.stage}" if self.stage else "")

    def stack(self, name: str) -> str:
        """DriveAtlas<Name> or DriveAtlas<Stage><Name>."""
        return "DriveAtlas" + self.stage.capitalize() + name

    def underscored(self, name: str) -> str:
        """For services that reject hyphens (AgentCore runtime names)."""
        return self.prefix.replace("-", "_") + "_" + name
