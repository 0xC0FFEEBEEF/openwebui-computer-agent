# ai-sandbox-agent

Lets a model drive a real desktop from an Open WebUI chat. Each chat gets its own session on the AI sandbox
VM: its own X desktop, its own Chrome profile, its own plan and goal, and a live viewer embedded in the chat.

| Folder | Runs on | What it is |
|---|---|---|
| `vm/` | the sandbox VM, as the unprivileged `agent` user | `sandbox_agent.py` is the control plane: sessions, screenshots with numbered elements, input, the noVNC viewer and its event stream. `install.sh` installs or updates everything (idempotent, run as root from this folder) and generates the tokens once. The `.service` files and `sandbox-egress.nft` are the units and the egress firewall it installs. |
| `owui/` | Open WebUI | `computer_agent.py` is the "Computer Agent" pipe (plan, act, verify loop). `computer_tool.py` is the tool form. `resume_chat.py` picks a chat's session back up. |

Tokens are generated on the VM by `install.sh` and are not in this repo.

## About this copy

A public snapshot of my private working repo; host names stand in for my lab's addresses. It powers the desktop side of [open-webui-code-mode](https://github.com/0xC0FFEEBEEF/open-webui-code-mode). More of my projects: [nbdevlab.com](https://www.nbdevlab.com).


## About this copy

A public snapshot of my private working repo; host names stand in for my lab's addresses. It powers the desktop side of [open-webui-code-mode](https://github.com/0xC0FFEEBEEF/open-webui-code-mode). More of my projects: [nbdevlab.com](https://www.nbdevlab.com).

## License

GNU General Public License v3.0 or later, see [`LICENSE`](LICENSE).

<sub>Preparing this public copy from my private repo was done with help from Claude.</sub>
