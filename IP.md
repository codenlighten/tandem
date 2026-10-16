# IP registrations

Records of authorship evidence for this project, made through iptrust.org. A record is evidence that the committed content existed by the time of the anchoring block and that the signer made the statement; it does not create or register any right.

| Date | What is covered | Type | Record | Signer | Git tag |
|---|---|---|---|---|---|
| 2026-10-16 | The public snapshot at the tagged commit (33 files): the whole platform as first published under the name tandem. Authors: Gregory J. Ward, Bryan W. Daugherty, Shawn M. Ryan. Hash only, unpublished. | software | [`f277c5a6b3564c23b5962ea1e15c9c7089f32930fbfbddc2bc792c8cb87d97ee`](https://iptrust.org/v/f277c5a6b3564c23b5962ea1e15c9c7089f32930fbfbddc2bc792c8cb87d97ee) | Gregory J. Ward | `ip-2026-10-16` |

## Verifying

The evidence (`.iptrust/`, with its private `opening.json` and `manifest.json`) is kept by the authors and is not in this repository. With it:

```bash
git worktree add /tmp/at-tag-tandem ip-2026-10-16 && cp -r .iptrust /tmp/at-tag-tandem/
(cd /tmp/at-tag-tandem && iptrust2 verify f277c5a6b3564c23b5962ea1e15c9c7089f32930fbfbddc2bc792c8cb87d97ee --fetch-headers)
git worktree remove --force /tmp/at-tag-tandem
```
