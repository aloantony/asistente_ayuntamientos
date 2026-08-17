"""Escudo municipal, guardado como referencia a un documento ya subido.

No se añade una columna de bytes ni una ruta suelta: el escudo es un fichero y
los ficheros de este producto viven en `documents`, con su control de acceso,
su checksum y su ciclo de archivado. Guardar aquí solo el identificador evita
un segundo almacén con reglas propias.
"""

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.documents.models import Document
    from app.organizations.models import Organization


class OrganizationBranding(TimestampMixin, Base):
    """Identidad visual de un ayuntamiento. Una fila por organización."""

    __tablename__ = "organization_branding"

    # La clave primaria es la propia organización: no caben dos escudos.
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    crest_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    crest_alt_text: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    organization: Mapped["Organization"] = relationship("Organization")
    crest_document: Mapped["Document | None"] = relationship("Document")
