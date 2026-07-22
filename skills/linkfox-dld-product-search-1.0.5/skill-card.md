## Description: <br>
Searches and analyzes 1688 wholesale product listings through DianLeiDa, including prices, sales metrics, supplier details, fulfillment filters, and product links. <br>

This skill is ready for commercial/non-commercial use. <br>

## Publisher: <br>
[linkfox-ai](https://clawhub.ai/user/linkfox-ai) <br>

### License/Terms of Use: <br>
MIT-0 <br>


## Use Case: <br>
External e-commerce sellers, sourcing agents, and procurement teams use this skill to find 1688 products and suppliers, compare wholesale and dropship pricing, filter by sales and supplier attributes, and review product opportunities. <br>

### Deployment Geography for Use: <br>
Global <br>

## Known Risks and Mitigations: <br>
Risk: The skill requires a LinkFox API key and can make paid external 1688 product-search requests. <br>
Mitigation: Use a scoped API key where possible, control the relevant environment variables, and confirm cost-sensitive searches before repeated or high-volume calls. <br>
Risk: Product queries and runtime context may be sent over the network to a third-party gateway. <br>
Mitigation: Pin or validate the gateway host, avoid sensitive search terms unless required, and review any API keys or session identifiers exposed to the runtime. <br>
Risk: Full API responses are retained locally and may include product, supplier, shop, URL, or query details from the user's task. <br>
Mitigation: Run the skill only in workspaces where local output paths are acceptable, review saved files, and remove retained responses when they are no longer needed. <br>
Risk: Feedback behavior can send user sentiment or issue details to a separate LinkFox feedback endpoint. <br>
Mitigation: Avoid sending sensitive feedback content and review feedback-related use in sensitive workspaces. <br>
Risk: Onboarding instructions may prompt installation of a related LinkFox onboarding skill when authentication or credits fail. <br>
Mitigation: Review any additional skill before installation and require explicit user approval before downloading or installing related materials. <br>


## Reference(s): <br>
- [1688 Product Search API Reference](references/api.md) <br>
- [ClawHub Skill Page](https://clawhub.ai/linkfox-ai/skills/linkfox-dld-product-search) <br>
- [LinkFox Skills](https://skill.linkfox.com/) <br>
- [LinkFox API Key Guide](https://skill.linkfox.com/linkfoxskills/guide.htm) <br>
- [LinkFox Account and Credits Portal](https://os.linkfox.com/) <br>


## Skill Output: <br>
**Output Type(s):** [Text, Markdown, JSON, Shell commands, Guidance] <br>
**Output Format:** [Markdown summaries and tables, shell commands, and JSON API results saved to local files or printed to stdout] <br>
**Output Parameters:** [1D] <br>
**Other Properties Related to Output:** [Full API responses are retained locally; large responses are summarized unless inline output is requested.] <br>

## Skill Version(s): <br>
1.0.5 (source: server release evidence) <br>

## Ethical Considerations: <br>
Users should evaluate whether this skill is appropriate for their environment, review any generated or modified files before relying on them, and apply their organization's safety, security, and compliance requirements before deployment. <br>
