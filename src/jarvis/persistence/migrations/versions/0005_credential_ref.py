"""credential_ref: rewrite agent version snapshots (S2, ADR 0006 §S2-migration)

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-06

One-shot JSONB data migration: `model.api_key_env: "X"` becomes
`model.credential_ref: {type: env, env_var: "X"}`. Only env-var *names* are
rewritten — plaintext never enters a snapshot. Snapshots without
`api_key_env` are left byte-identical (ADR 0002: immutable history).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Pre-S2 serialization wrote `api_key_env: null` (explicit JSON null) for
    # credential-less models — a null the removed domain field can no longer
    # express (extra="forbid"). Drop those keys first: they carry no
    # reference. Match on VALUE, never key presence (`?` matches nulls too —
    # the original bug, which produced credential_ref {type: env,
    # env_var: null} and 500s on every affected listing).
    op.execute(
        """
        UPDATE agent_versions
        SET snapshot = snapshot #- '{model,api_key_env}'::text[]
        WHERE snapshot -> 'model' ? 'api_key_env'
          AND coalesce(snapshot -> 'model' ->> 'api_key_env', '') = ''
        """
    )
    # Self-heal for databases already migrated by the pre-fix form of this
    # migration (its WHERE matched api_key_env: null): remove the bogus
    # credential_ref blobs it wrote. No-op on fresh databases.
    op.execute(
        """
        UPDATE agent_versions
        SET snapshot = snapshot #- '{model,credential_ref}'::text[]
        WHERE snapshot #>> '{model,credential_ref,type}'::text[] = 'env'
          AND coalesce(snapshot #>> '{model,credential_ref,env_var}'::text[], '') = ''
        """
    )
    # NOTE: explicit ::text[] casts — asyncpg does not infer array types
    # from string literals.
    op.execute(
        """
        UPDATE agent_versions
        SET snapshot = jsonb_set(
                snapshot #- '{model,api_key_env}'::text[],
                '{model,credential_ref}'::text[],
                jsonb_build_object(
                    'type', 'env',
                    'env_var', snapshot -> 'model' ->> 'api_key_env'
                )
            )
        WHERE coalesce(snapshot -> 'model' ->> 'api_key_env', '') <> ''
        """
    )


def downgrade() -> None:
    # Env references convert back to api_key_env; stored credential refs
    # have no pre-S2 representation and are dropped (downgrade is lossy for
    # BYOK references — they cannot exist meaningfully without the S2
    # tenant model anyway).
    op.execute(
        """
        UPDATE agent_versions
        SET snapshot = jsonb_set(
                snapshot #- '{model,credential_ref}'::text[],
                '{model,api_key_env}'::text[],
                to_jsonb(snapshot -> 'model' -> 'credential_ref' ->> 'env_var')
            )
        WHERE snapshot -> 'model' -> 'credential_ref' ->> 'type' = 'env'
        """
    )
    op.execute(
        """
        UPDATE agent_versions
        SET snapshot = snapshot #- '{model,credential_ref}'::text[]
        WHERE snapshot -> 'model' -> 'credential_ref' ->> 'type' = 'stored'
        """
    )
    # Self-heal (see upgrade): drop credential_ref blobs with a null env_var
    # before the env rewrite reads them.
    op.execute(
        """
        UPDATE agent_versions
        SET snapshot = snapshot #- '{model,credential_ref}'::text[]
        WHERE snapshot #>> '{model,credential_ref,type}'::text[] = 'env'
          AND coalesce(snapshot #>> '{model,credential_ref,env_var}'::text[], '') = ''
        """
    )
