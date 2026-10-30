# Docker Build and Publish

**Latest version:** [v0.1.0](https://github.com/btnguyen2k/gh-actions/releases/tag/dockerbp/v0.1.0)
(tag: `dockerbp/v0.1.0`).

Build Linux multi-architecture images or native Windows images from an exact
source commit, optionally publish to Docker Hub, and combine builds from separate
jobs into one set of image tags.

## Quick start

The examples below use an action-prefixed version tag. For reproducibility
and stronger supply-chain security, prefer replacing the tag in `uses` with
that release's full commit SHA.

No caller checkout is required. The action checks out the **caller repository**
at `target-sha` into a private directory, verifies its HEAD, and removes that
directory afterward. Uncommitted files and files generated in the caller's
workspace are not part of this pinned build context.

```yaml
permissions:
  contents: read

jobs:
  image:
    runs-on: ubuntu-latest
    steps:
      - uses: btnguyen2k/gh-actions/dockerbp@dockerbp/v0.1.0
        with:
          image: myorg/myapp
          platforms: linux/amd64,linux/arm64
```

By default this **builds only**, retaining builder cache without loading or
exporting a usable image. To publish, log in first and set `publish: true`:

```yaml
steps:
  - uses: docker/login-action@81c6f93d4bc991e510b342a00b419b3bb4efc5c6 # v4.7.0
    with:
      username: ${{ vars.DOCKERHUB_USERNAME }}
      password: ${{ secrets.DOCKERHUB_TOKEN }}
  - uses: btnguyen2k/gh-actions/dockerbp@dockerbp/v0.1.0
    id: image
    with:
      mode: build
      image: myorg/myapp
      platforms: linux/amd64,linux/arm64
      tags: |
        0.1.0
        latest
      publish: true
```

Every final publication includes `<image>:<full-pinned-SHA>`, in addition to
the requested tags. Only Docker Hub is supported. The action does not log in,
create repositories, or manage registry credentials.

## Inputs

Lists accept comma-separated or multiline values. Empty lists use their defaults;
duplicate entries are removed.

| Input | Default | Description |
| --- | --- | --- |
| `image` | Required | Lowercase Docker Hub name, for example `myorg/myapp` or `docker.io/myorg/myapp`, without a tag/digest. A bare repository name uses `library/`. |
| `target-sha` | `github.sha` | Full 40-character source commit SHA. Branch names and short SHAs are rejected. |
| `tags` | `latest` | Bare image tags. The full pinned SHA is always added. In `build-shard`, these are planned final references, not published tags. |
| `platforms` | `linux/amd64` | Platforms to build; in `merge`, the complete expected platform set. Recognized values: `linux/amd64`, `linux/arm64`, `windows/amd64`, `windows/arm64`. See runner requirements below. |
| `publish` | `false` | Build only when false; publish when true. Must be `true` or `false`, case-insensitive. |
| `dockerfile` | `Dockerfile` | Dockerfile path relative to `context`. |
| `context` | `./` | Directory relative to the pinned checkout. Absolute paths, parent traversal, and symlinks escaping the checkout/context are rejected. |
| `mode` | `build` | `build`, `build-shard`, or `merge`, as described below. |
| `build-records` | Empty | File or directory containing downloaded JSON build records. Required for `merge`. Relative paths resolve from `github.workspace`; absolute paths are accepted. |

## Modes and outputs

| Mode | Behavior |
| --- | --- |
| `build` | Build one job's platforms. With `publish: true`, push a unique staging tag and publish the final tags. |
| `build-shard` | Build one job's platforms. With `publish: true`, push only a unique staging tag and return a transferable result file. Never changes final tags. |
| `merge` | Validate published result files and their immutable registry manifests, then publish the final combined tags. Requires `publish: true`; does not check out source or need a Docker daemon. |

`publish: false` never pushes images or tags. Linux uses Buildx's cache-only
exporter. The native Windows legacy builder necessarily creates a temporary local
image; the action removes its private tag with `--no-prune`, retaining intermediate
builder cache. Neither backend exports an archive or leaves a tagged runnable
image. Cache is job/daemon-local, not a cross-job image transport.
Linux cache is not exported and may be removed by Buildx's post-job cleanup.

| Output | Format |
| --- | --- |
| `status` | Currently `built`, `shard-published`, or `published` on success. Future versions may add status values; see the error-reporting note below. |
| `image` | Canonical `docker.io/<namespace>/<repository>`. |
| `target-sha` | Full pinned SHA. |
| `tags` | Newline-separated final image references, including the SHA tag. Planned only when build-only or in `build-shard`. |
| `digest` | Published manifest-list digest; a shard returns its own image/index digest. Empty when build-only. |
| `record-file` | Absolute JSON result path for a build; empty in `merge`. Upload this file, not its parent directory. Build-only records cannot be merged. |
| `staging-tags` | Newline-separated staging references for caller-managed retention. Empty when build-only. |

**Error reporting:** Fatal errors currently fail the action; this version does
not return a dedicated error status. Error reporting and output status values
may change in future versions, for example by also returning `status: error`.
Use the action step's outcome to detect failures, do not assume outputs are
populated after a failure, and follow the documented contract of your pinned
version.

## Examples by mode

Choose based on **single-job versus distributed or deferred publication**, not
just the number of platforms. `mode: build` supports **both Linux and Windows**;
it can also build Linux AMD64 and ARM64 together in one job. Use a native runner
and Docker daemon for the chosen OS; do not combine Linux and Windows in one
build invocation.

| Use case | Modes |
| --- | --- |
| One Linux platform or one native Windows architecture/base-OS version | `build` |
| Linux AMD64 + ARM64 from one job, using emulation when needed | `build` |
| Native Linux AMD64 + ARM64 builds on separate runners | `build-shard` then `merge` |
| Linux + Windows under the same image tags | `build-shard` then `merge` |
| Build first, then apply final tags after validation or approval, even for one platform | `build-shard` then `merge` |

The shard/merge publication flow requires `publish: true`: shards publish staging
images, while `merge` alone updates final tags. Build-only records cannot be
merged. See [Distributed builds](#distributed-builds) for complete Linux-only
and Linux/Windows workflows.

### `build`: check Linux images without publishing

Use an ephemeral GitHub-hosted Linux runner to check both Linux architectures.
No Docker Hub login, publishing token, artifact transfer, or merge job is needed.
For pull requests, the default `github.sha` checks the PR merge commit rather
than just the source branch's head.

```yaml
name: Check Docker image
on: pull_request

permissions:
  contents: read

jobs:
  image:
    runs-on: ubuntu-latest
    steps:
      - uses: btnguyen2k/gh-actions/dockerbp@dockerbp/v0.1.0
        with:
          mode: build
          image: myorg/myapp
          platforms: linux/amd64,linux/arm64
          publish: false
```

The result is `status: built` with an empty `digest`. Cache-only builds do not
leave an image for a subsequent `docker run`, and their records cannot be merged.

### `build`: check a Windows image without publishing

`build` supports Windows images in a single native Windows job, either build-only
or with direct publication. The Docker daemon must run Windows containers and
match the requested architecture. Each Windows job builds one architecture.

This workflow checks Windows AMD64 using a compatible `Dockerfile.windows` in
the caller repository, without login, artifact transfer, or a merge job:

```yaml
name: Build Windows image
on: pull_request

permissions:
  contents: read

jobs:
  image:
    runs-on: windows-latest
    steps:
      - uses: btnguyen2k/gh-actions/dockerbp@dockerbp/v0.1.0
        with:
          mode: build
          image: myorg/myapp-windows
          platforms: windows/amd64
          dockerfile: Dockerfile.windows
          publish: false
```

Windows jobs must set `platforms` explicitly because the default is
`linux/amd64`. This build-only run returns `status: built` without a published
digest or a retained tagged runnable image.

To publish directly from this Windows job, use a trusted push or manual trigger
instead of untrusted pull requests, log in to Docker Hub before the action, and
set `publish: true`. `build` then publishes the requested final tags plus the
full-SHA tag; no `build-shard` or `merge` invocation is needed.

### `build`: select a source commit and custom paths

This manually triggered workflow builds an exact commit supplied by the caller.
The commit must belong to the caller repository and contain
`services/api/docker/Dockerfile`. The Dockerfile path is relative to `context`,
not to the repository root.

```yaml
name: Build selected source
on:
  workflow_dispatch:
    inputs:
      source-sha:
        description: Full 40-character source commit SHA
        type: string
        required: true

permissions:
  contents: read

jobs:
  image:
    runs-on: ubuntu-latest
    steps:
      - uses: btnguyen2k/gh-actions/dockerbp@dockerbp/v0.1.0
        with:
          mode: build
          image: myorg/api
          target-sha: ${{ inputs.source-sha }}
          context: services/api
          dockerfile: docker/Dockerfile
          publish: false
```

This uses the default `linux/amd64` platform. To publish the selected source,
add Docker Hub login before the action and set `publish: true`; its automatic
SHA tag will use `source-sha`, not the workflow run's original `github.sha`.
The action version in `uses` and the application commit in `target-sha` are
independent pins.

### `build-shard`: stage a single-platform image before approval

The following two examples are job definitions to place under `jobs` in the
same workflow, triggered by a trusted push or manual dispatch. This Windows-only
example deliberately separates building from final publication; it does not
need multiple platforms. Without a validation or approval gate, use `build`
instead. The build job requires a compatible `Dockerfile.windows` in the caller
repository.

```yaml
build-windows:
  runs-on: windows-latest
  permissions:
    contents: read
  steps:
    - uses: docker/login-action@v4.7.0
      with:
        username: ${{ vars.DOCKERHUB_USERNAME }}
        password: ${{ secrets.DOCKERHUB_TOKEN }}
    - uses: btnguyen2k/gh-actions/dockerbp@dockerbp/v0.1.0
      id: shard
      with:
        mode: build-shard
        image: myorg/myapp-windows
        target-sha: ${{ github.sha }}
        platforms: windows/amd64
        dockerfile: Dockerfile.windows
        publish: true
    - uses: actions/upload-artifact@v7.0.2
      with:
        name: dockerbp-windows
        path: ${{ steps.shard.outputs.record-file }}
        if-no-files-found: error
```

This returns `status: shard-published`. The registry receives only the unique
staging tag; neither `latest` nor the final full-SHA tag is changed yet.
Upload the result JSON, not the image itself. The record contains immutable image
digests that the merge job can read from Docker Hub.

### `merge`: publish after approval without rebuilding

Add this job alongside `build-windows` above. The Windows job builds the image;
this Linux job downloads its record and applies the final tags without
rebuilding or needing a Windows Docker daemon.

Configure required reviewers on the `dockerhub-publish` GitHub environment to
pause this job for approval; naming the environment alone does not require
approval. The gate protects final-tag publication, not staging-image publication.
For automated validation instead, make this job also depend on a successful
validation job that checks the staged image.

```yaml
merge-windows:
  needs: build-windows
  runs-on: ubuntu-latest
  environment: dockerhub-publish
  permissions:
    contents: read
  steps:
    - uses: actions/download-artifact@v8.0.2
      with:
        name: dockerbp-windows
        path: build-records
    - uses: docker/login-action@v4.7.0
      with:
        username: ${{ vars.DOCKERHUB_USERNAME }}
        password: ${{ secrets.DOCKERHUB_TOKEN }}
    - uses: btnguyen2k/gh-actions/dockerbp@dockerbp/v0.1.0
      id: image
      with:
        mode: merge
        image: myorg/myapp-windows
        target-sha: ${{ github.sha }}
        platforms: windows/amd64
        tags: windows
        publish: true
        build-records: build-records
```

The final references are `myorg/myapp-windows:windows` and
`myorg/myapp-windows:<full-pinned-SHA>`, with `status: published` and the final
manifest-list `digest`. No `latest` tag is added because `tags` is nonempty.
Both jobs must agree on `image` and `target-sha`; `platforms` in `merge` must
match exactly what the downloaded records contain.

One record is sufficient, as in this Windows-only example. Use workflow-level
publication concurrency as shown below. To combine several results into the
same image, use either distributed workflow.

## Distributed builds

A composite action runs in **one caller job**; it cannot start additional runner
jobs. Distributed builds are useful even for Linux-only images: build
architectures natively in parallel, avoid emulation, or use platform-specific
Dockerfiles and contexts. They are also required to combine Linux and Windows
under the same image tags.

Use `build-shard` on each compatible runner, transfer its JSON result with
artifacts, and let a final `merge` job own all final tags. All shards must agree
on `image` and `target-sha`; the merge job's `platforms` must be the complete
expected set. The following workflows are alternatives, not jobs to combine.

### Linux-only: native AMD64 and ARM64 builds

This workflow uses `ubuntu-24.04` for AMD64 and `ubuntu-24.04-arm` for ARM64,
building both natively without QEMU. It assumes `Dockerfile` supports both
architectures. Substitute compatible self-hosted runners if needed.

```yaml
name: Publish native Linux image
on:
  push:
    branches: [main]

permissions:
  contents: read

concurrency:
  group: dockerbp-myapp-publish
  cancel-in-progress: false

jobs:
  build:
    strategy:
      fail-fast: false
      matrix:
        include:
          - name: amd64
            runner: ubuntu-24.04
            platform: linux/amd64
          - name: arm64
            runner: ubuntu-24.04-arm
            platform: linux/arm64
    runs-on: ${{ matrix.runner }}
    steps:
      - uses: docker/login-action@v4.7.0
        with:
          username: ${{ vars.DOCKERHUB_USERNAME }}
          password: ${{ secrets.DOCKERHUB_TOKEN }}
      - uses: btnguyen2k/gh-actions/dockerbp@dockerbp/v0.1.0
        id: shard
        with:
          mode: build-shard
          image: myorg/myapp
          target-sha: ${{ github.sha }}
          platforms: ${{ matrix.platform }}
          publish: true
      - uses: actions/upload-artifact@v7.0.2
        with:
          name: dockerbp-${{ matrix.name }}
          path: ${{ steps.shard.outputs.record-file }}
          if-no-files-found: error

  merge:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - uses: actions/download-artifact@v8.0.2
        with:
          pattern: dockerbp-*
          path: build-records
          merge-multiple: true
      - uses: docker/login-action@v4.7.0
        with:
          username: ${{ vars.DOCKERHUB_USERNAME }}
          password: ${{ secrets.DOCKERHUB_TOKEN }}
      - uses: btnguyen2k/gh-actions/dockerbp@dockerbp/v0.1.0
        id: image
        with:
          mode: merge
          image: myorg/myapp
          target-sha: ${{ github.sha }}
          platforms: linux/amd64,linux/arm64
          tags: |
            0.1.0
            latest
          publish: true
          build-records: build-records
```

### Linux and Windows under shared tags

This example builds Linux AMD64/ARM64 and Windows AMD64. It assumes the source
contains `Dockerfile` and a compatible `Dockerfile.windows`.

```yaml
name: Publish image
on:
  push:
    branches: [main]

permissions:
  contents: read

concurrency:
  group: dockerbp-myapp-publish
  cancel-in-progress: false

jobs:
  build:
    strategy:
      fail-fast: false
      matrix:
        include:
          - name: linux
            runner: ubuntu-latest
            platforms: linux/amd64,linux/arm64
            dockerfile: Dockerfile
          - name: windows
            runner: windows-latest
            platforms: windows/amd64
            dockerfile: Dockerfile.windows
    runs-on: ${{ matrix.runner }}
    steps:
      - uses: docker/login-action@v4.7.0
        with:
          username: ${{ vars.DOCKERHUB_USERNAME }}
          password: ${{ secrets.DOCKERHUB_TOKEN }}
      - uses: btnguyen2k/gh-actions/dockerbp@dockerbp/v0.1.0
        id: image
        with:
          mode: build-shard
          image: myorg/myapp
          target-sha: ${{ github.sha }}
          platforms: ${{ matrix.platforms }}
          dockerfile: ${{ matrix.dockerfile }}
          publish: true
      - uses: actions/upload-artifact@v7.0.2
        with:
          name: dockerbp-${{ matrix.name }}
          path: ${{ steps.image.outputs.record-file }}
          if-no-files-found: error

  merge:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - uses: actions/download-artifact@v8.0.2
        with:
          pattern: dockerbp-*
          path: build-records
          merge-multiple: true
      - uses: docker/login-action@v4.7.0
        with:
          username: ${{ vars.DOCKERHUB_USERNAME }}
          password: ${{ secrets.DOCKERHUB_TOKEN }}
      - uses: btnguyen2k/gh-actions/dockerbp@dockerbp/v0.1.0
        id: image
        with:
          mode: merge
          image: myorg/myapp
          target-sha: ${{ github.sha }}
          platforms: linux/amd64,linux/arm64,windows/amd64
          tags: |
            0.1.0
            latest
          publish: true
          build-records: build-records
```

### Shared rules for distributed publication

Result filenames are unique, so downloading multiple artifacts into one directory
does not overwrite them. Merge accepts one to four schema-1 JSON files, each at
most 1 MiB, and rejects symbolic links. Every expected OS/architecture pair must
occur exactly once. Wrong images/SHAs, unpublished records, unsupported schemas,
missing or duplicate platforms, and metadata that differs from the registry all fail
**before any final tag is pushed**. Windows `os.version`, OS features, and
architecture variants are preserved.

Only the merge job should publish final tags in a distributed build. Different
runs targeting the same final tags still need caller-owned workflow concurrency.
Each example's workflow-level group serializes this image's publication; use the
same group in other publishing workflows that write the same image tags.
Updating several Docker tags is not transactional: a registry/network failure
can leave only some tags updated. Re-run the merge with the same immutable
records after resolving the error.

Skip the merge job for build-only workflows: cache-only results cannot be merged.
The action does not upload/download artifacts itself, delete registry tags, or
transfer image archives. Staging tags have the form
`dockerbp-<SHA>-<platforms>-<random-suffix>` and remain after publication, including
if a later merge fails. Manage them with your own registry retention/cleanup
process when no longer needed.

## Other scenarios

### Choose image tags without accidentally publishing `latest`

These are application image tags, independent of the DockerBP action version.
Every final publication also includes the full pinned source SHA.

| Goal | `tags` input | Tags besides the automatic SHA tag |
| --- | --- | --- |
| Default publication | Omit it or use an empty value | `latest` |
| Version aliases without `latest` | `1.4.0,1.4,1` | `1.4.0`, `1.4`, `1` |
| Canary image | `canary` | `canary` |
| Only the default source SHA | `${{ github.sha }}` | None; the duplicate SHA is removed |

For SHA-only publication with a custom `target-sha`, set `tags` to that same
source SHA rather than `github.sha`. Empty `tags` does **not** disable `latest`.
In a distributed build, choose final tags in `merge`; `build-shard` never
applies them.

### Consume the final immutable image reference

After a publishing `build` or `merge` step with `id: image`, use `image` and
`digest` to identify the final manifest independently of mutable tags:

```yaml
- name: Report published image
  shell: bash
  env:
    IMAGE: ${{ steps.image.outputs.image }}
    DIGEST: ${{ steps.image.outputs.digest }}
  run: |
    printf 'ℹ️ Published image: %s@%s\n' "$IMAGE" "$DIGEST"
```

Use the **merge step's** digest for a distributed image, not one build shard's
digest. Build-only runs have no published digest. Likewise, do not collect
matrix shards through one shared job output: transfer each result file as an
artifact so one matrix result cannot overwrite another.

### Validate distributed builds without publishing

To turn either distributed workflow into a build-only check, set every
matrix invocation to `mode: build` and `publish: false`, and remove login,
artifact upload, and the merge job. Each native job validates its own build.
Do not invoke `merge` with `publish: false` or with cache-only records.

### Target multiple Windows Server versions

Windows Server 2019 and 2022 are OS-version variants, not different CPU
architectures. For process-isolated support on both, normally build separate
images from the corresponding Windows base images on compatible builders.
Follow Microsoft's [host/image compatibility requirements](https://learn.microsoft.com/en-us/virtualization/windowscontainers/deploy-containers/version-compatibility).

**Current limitation:** DockerBP cannot merge two Windows versions of the same
architecture. Both builds declare `windows/amd64`, so coverage validation rejects
them as duplicate platforms even when their `os.version` values differ.
Preserving `os.version` does not mean multi-version merging is supported.

For separate publications today, use distinct image repositories such as
`myorg/myapp-windows2019` and `myorg/myapp-windows2022` with `mode: build`.
Version-specific tags in one repository do not fully isolate these publications:
each build also updates the mandatory `<image>:<full-pinned-SHA>` tag.
A shared tag containing both Windows versions requires a custom manifest
publication flow or future DockerBP support.

## Runner requirements and compatibility

All modes require Bash and Node.js 22+ on `PATH`, plus the Docker CLI.
GitHub-hosted Linux/Windows runners provide these; self-hosted Windows runners
need Git for Windows Bash, not WSL. Build modes also need Git and a working
Docker daemon. The source checkout needs `contents: read` through the caller's
`GITHUB_TOKEN`; no write permission or personal token is required.
Use a current Actions runner: the pinned checkout/setup helpers use Node.js 24.

| Platform | Requirements |
| --- | --- |
| `linux/amd64`, `linux/arm64` | Native Linux runner with a Linux Docker daemon. The action provisions Buildx and QEMU when the requested architecture differs from the daemon. QEMU setup requires privileged container support. Both platforms may share one job. |
| `windows/amd64` | Native Windows runner with Docker in Windows-container mode and a compatible Windows base image. One architecture per job. CI covers the `windows-latest` host with Nano Server `ltsc2025`. |
| `windows/arm64` | Recognized, but requires an actually capable native Windows ARM64 container daemon and compatible base images. Standard GitHub-hosted Windows ARM64 images do not provide this setup. No Windows ARM64 execution coverage is claimed. |
| Merge | Any runner meeting the CLI/Bash/Node requirements. Registry operations do not require a Docker daemon. |

`windows-latest` is AMD64, not a route to emulating Windows ARM64. Linux QEMU
cannot build Windows containers. Windows builds use Docker's native legacy
builder with `DOCKER_BUILDKIT=0`; experimental Windows BuildKit is not required.
Docker's `manifest` CLI, used for registry-only merging, is itself marked
experimental by Docker.

Linux publication uses Docker schema-2 manifests without provenance/SBOM
attestations so native Windows and Linux results can be combined consistently.
The result JSON is build metadata, **not a signed provenance attestation**.
Only merge artifacts from trusted build jobs for the intended source SHA.

Builds execute Dockerfile instructions with access to the caller's Docker daemon.
Use ephemeral runners for untrusted source, never expose publishing credentials
to untrusted pull requests, and log in separately in every publishing job.
Credentials stay in Docker's credential handling and a private temporary config;
they are never placed in result files. Temporary config and source checkout are
removed even after a later action step fails.

## License

This action is licensed under the MIT License. See
[LICENSE.md](https://github.com/btnguyen2k/gh-actions/blob/main/LICENSE.md).
