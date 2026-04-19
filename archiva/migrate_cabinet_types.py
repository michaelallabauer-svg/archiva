from __future__ import annotations

from sqlalchemy import text

from archiva.config import load_settings
from archiva.database import init_db, get_session


def main() -> None:
    settings = load_settings("config.yaml")
    init_db(settings)

    with get_session() as session:
        has_cabinet_types = session.execute(
            text("SELECT 1 FROM information_schema.tables WHERE table_name = 'cabinet_types' LIMIT 1")
        ).first()
        if not has_cabinet_types:
            session.execute(
                text(
                    """
                    CREATE TABLE cabinet_types (
                        id UUID PRIMARY KEY,
                        name VARCHAR(255) NOT NULL UNIQUE,
                        description TEXT NULL,
                        "order" INTEGER NOT NULL DEFAULT 0,
                        created_at TIMESTAMP NOT NULL DEFAULT now(),
                        updated_at TIMESTAMP NOT NULL DEFAULT now()
                    )
                    """
                )
            )

        has_cabinet_type_id = session.execute(
            text(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'cabinets' AND column_name = 'cabinet_type_id'
                LIMIT 1
                """
            )
        ).first()
        if not has_cabinet_type_id:
            session.execute(text("ALTER TABLE cabinets ADD COLUMN cabinet_type_id UUID NULL"))

        fallback_type = session.execute(
            text("SELECT id FROM cabinet_types WHERE name = 'Bestand' LIMIT 1")
        ).first()
        if fallback_type:
            fallback_type_id = fallback_type[0]
        else:
            fallback_type_id = session.execute(
                text(
                    """
                    INSERT INTO cabinet_types (id, name, description, "order", created_at, updated_at)
                    VALUES ('00000000-0000-0000-0000-000000000001', 'Bestand', 'Automatisch erzeugter Cabinettyp für bestehende Cabinets', 0, now(), now())
                    RETURNING id
                    """
                )
            ).scalar_one()

        session.execute(
            text(
                """
                UPDATE cabinets
                SET cabinet_type_id = :fallback_type_id
                WHERE cabinet_type_id IS NULL
                """
            ),
            {"fallback_type_id": fallback_type_id},
        )

        session.execute(
            text(
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1
                        FROM information_schema.table_constraints
                        WHERE table_name = 'cabinets' AND constraint_name = 'fk_cabinets_cabinet_type_id'
                    ) THEN
                        ALTER TABLE cabinets
                        ADD CONSTRAINT fk_cabinets_cabinet_type_id
                        FOREIGN KEY (cabinet_type_id) REFERENCES cabinet_types(id);
                    END IF;
                END$$;
                """
            )
        )

        session.execute(text("ALTER TABLE cabinets ALTER COLUMN cabinet_type_id SET NOT NULL"))

    print("CabinetType migration completed.")


if __name__ == "__main__":
    main()
