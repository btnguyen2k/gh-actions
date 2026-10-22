[![Release](https://img.shields.io/github/release/btnguyen2k/gh-actions.svg?style=flat-square)](RELEASE-NOTES.md)
[![Actions Status](https://github.com/btnguyen2k/gh-actions/actions/workflows/ci.yaml/badge.svg)](https://github.com/btnguyen2k/gh-actions/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](LICENSE.md)

Reusable GitHub actions to reduce repetitive workflow scripting and standardize
automation across repositories.

Each action lives in its own directory and is versioned independently using
**action-prefixed tags**, so you can adopt updates without coupling unrelated
workflows.

## Actions

| Action | Benefit |
| --- | --- |
| [workflow-context](workflow-context/README.md) | Standardize workflow context and share event, SHA/branch, PR, and run metadata across jobs. |

See each action's README for inputs, outputs, permissions, and supported runners.

## Using an action

Replace `<action-name>` with an action's directory name:

| Reference | Example | Behavior |
| --- | --- | --- |
| Full version | `btnguyen2k/gh-actions/<action-name>@<action-name>/v1.2.3` | Fixed release; never moved or reused |
| Major alias | `btnguyen2k/gh-actions/<action-name>@<action-name>/v1` | Latest stable release in major version 1 |
| Minor alias | `btnguyen2k/gh-actions/<action-name>@<action-name>/v1.2` | Latest stable release in minor version 1.2 |
| Commit SHA | `btnguyen2k/gh-actions/<action-name>@<full-commit-sha>` | Exact repository snapshot; strongest pinning |

For production workflows, prefer the full commit SHA of the selected action's
release, with a comment identifying its version. A tag always points to a snapshot
of the **whole repository**: another action's release tag is not a release of the
action you intend to use. There are no repository-wide `v1` or `v1.2.3` tags.

Subdirectory actions can be consumed directly without a Marketplace listing.
GitHub Marketplace requires root-level action metadata, so this layout does not
provide a separate Marketplace listing for each action. See
[GitHub's publishing requirements](https://docs.github.com/en/actions/how-tos/create-and-publish-actions/publish-in-github-marketplace).

## License

This project is licensed under the MIT License. See the [LICENSE.md](LICENSE.md) file for details.

## Support and Contribution

Feel free to create [pull requests](https://github.com/btnguyen2k/gh-actions/pulls) or [issues](https://github.com/btnguyen2k/gh-actions/issues) to report bugs or suggest new features.
Please search the existing issues before filing new ones to avoid duplicates. For new issues, file your bug or feature request as a new issue.

Contributor guidance: [AGENTS.md](AGENTS.md).

If you find this project useful, please star it.
