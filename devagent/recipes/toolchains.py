# devagent/recipes/toolchains.py
"""Declarative toolchain images (M11). A recipe manifest registers a stack as data, but a NEW
toolchain it names (a JDK+Maven image, a Rust image, ...) must still be BUILT before the
sandbox can use it. This closes that gap: a manifest's `toolchain.dockerfile` names a Dockerfile
(relative to the recipes dir) that builds `toolchain.image`, so adding a toolchain is a manifest
+ a Dockerfile — never a dev-agent code edit.

`toolchain_build_specs` maps the manifests under a recipes dir to docker-build specs (pure, unit-
tested); `build_all` runs them. `sandbox/build.sh recipes` is the operator entrypoint."""

import os
import subprocess
import sys
from pathlib import Path

from .registry import load_external_recipes


def toolchain_build_specs(directory) -> list[dict]:
    """The docker-build specs for every distinct toolchain image declared with a Dockerfile under
    *directory*. Recipes whose toolchain is prebuilt (no `dockerfile`) contribute nothing. Paths
    resolve relative to the recipes dir; deduped by image (one Dockerfile may back many recipes)."""
    d = Path(directory)
    recipes = load_external_recipes(d)            # raises loudly (naming the file) on a bad manifest
    seen: set[str] = set()
    specs: list[dict] = []
    for r in recipes.values():
        tc = r.toolchain
        if not tc.dockerfile or tc.image in seen:
            continue
        seen.add(tc.image)
        dockerfile = (d / tc.dockerfile).resolve()
        context = (d / tc.build_context).resolve() if tc.build_context else dockerfile.parent
        specs.append({"image": tc.image, "dockerfile": str(dockerfile), "context": str(context)})
    return specs


def build_all(directory, runner=subprocess.run) -> list[str]:
    """Build every declared toolchain image. Returns the images built, in manifest order."""
    built = []
    for s in toolchain_build_specs(directory):
        runner(["docker", "build", "-f", s["dockerfile"], "-t", s["image"], s["context"]], check=True)
        built.append(s["image"])
    return built


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else os.getenv("DEVAGENT_RECIPES_DIR")
    if not target:
        print("usage: python -m devagent.recipes.toolchains <recipes-dir>", file=sys.stderr)
        sys.exit(2)
    images = build_all(target)
    print(f"built {len(images)} toolchain image(s): {', '.join(images) or '(none declared)'}")
