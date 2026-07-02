"""Application services shared by the two presentation adapters.

The L3.5 layer from the architecture plan (docs/developers/architecture-plan.md):
policy and orchestration that must stay behaviorally identical between the CLI
and the hub live here, below both adapters. The hub must not import the cli, so
a shared home underneath them is the only shape that doesn't duplicate — this
package is that home. It imports NO adapter (enforced by the app-below-adapters
import-linter contract).
"""
