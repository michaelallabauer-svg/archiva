"""Create or update the initial Archiva admin user.

Useful for Docker/Compose installs where the database container is already
running and Archiva should be bootstrapped without the full local shell installer.
"""

from __future__ import annotations

import os
import uuid

from argon2 import PasswordHasher
from sqlalchemy import text

from archiva.config import load_settings
from archiva.database import get_session, init_db
from archiva.models import Role, User, UserRoleAssignment


def main() -> None:
    config_path = os.environ.get("ARCHIVA_CONFIG", "config.yaml")
    email = os.environ.get("ARCHIVA_ADMIN_EMAIL", "admin@archiva.local").strip().lower()
    name = os.environ.get("ARCHIVA_ADMIN_NAME", "Archiva Admin").strip() or email
    password = os.environ.get("ARCHIVA_ADMIN_PASSWORD", "")
    password_hash = PasswordHasher().hash(password) if password else None

    settings = load_settings(config_path)
    init_db(settings)

    with get_session() as db:
        admin_role = db.query(Role).where(Role.name == "Admin").first()
        if admin_role is None:
            admin_role = Role(
                name="Admin",
                description="Voller Zugriff auf Administration und Systemkonfiguration",
                is_system=True,
                permissions_json='["admin:*", "app:*", "workflow:*", "identity:*"]',
            )
            db.add(admin_role)
            db.flush()

        user = db.query(User).where(User.email == email).first()
        if user is None:
            user = User(
                email=email,
                display_name=name,
                auth_source="local",
                status="active",
                password_hash=password_hash,
            )
            db.add(user)
            db.flush()
        else:
            user.display_name = name
            user.status = "active"
            user.password_hash = password_hash

        exists = (
            db.query(UserRoleAssignment)
            .where(UserRoleAssignment.user_id == user.id, UserRoleAssignment.role_id == admin_role.id)
            .first()
        )
        if exists is None:
            db.add(UserRoleAssignment(user_id=user.id, role_id=admin_role.id))

        db.execute(
            text(
                """
                INSERT INTO assignment_targets (id, target_type, user_id, label, description, created_at, updated_at)
                VALUES (:id, 'user', :user_id, :label, :description, NOW(), NOW())
                ON CONFLICT DO NOTHING
                """
            ),
            {
                "id": uuid.uuid4(),
                "user_id": user.id,
                "label": user.display_name,
                "description": user.email,
            },
        )
        db.commit()

    password_label = "password set" if password else "empty password for first bootstrap login"
    print(f"Initial admin user: {email} ({password_label})")


if __name__ == "__main__":
    main()
