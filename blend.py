"""Mezclas de pareja ("blend"): playlists hechas con el historial de dos cuentas.

Privacidad: el historial de otra persona solo se usa si las dos cuentas están ENLAZADAS
con consentimiento mutuo (una invita, la otra acepta). Cualquiera de las dos puede romper
el enlace y, desde ese momento, sus mezclas dejan de calcularse.
"""
from __future__ import annotations

import time

from db import Database
from history import TopTrack

MODES = {
    "comunes": "en común",          # canciones que escucháis los dos
    "mitad": "mitad y mitad",       # se alternan los favoritos de cada uno
}


def invite(db: Database, from_user: str, to_user: str) -> str:
    """Invita a ``to_user``. Si ya te había invitado él/ella, queda aceptado directamente.

    Devuelve el estado resultante: 'pending' o 'accepted'.
    """
    to_user = to_user.strip()
    if not to_user or to_user == from_user:
        raise ValueError("Indica el ID de Spotify de otra persona.")
    if is_linked(db, from_user, to_user):
        return "accepted"
    if accept(db, from_user, to_user):  # ya existía su invitación hacia mí
        return "accepted"
    db.execute(
        "INSERT INTO blend_links (owner, partner, status, created_at) VALUES (?, ?, 'pending', ?) "
        "ON CONFLICT (owner, partner) DO NOTHING",
        (from_user, to_user, int(time.time())),
    )
    db.commit()
    return "pending"


def accept(db: Database, user: str, from_user: str) -> bool:
    """``user`` acepta la invitación de ``from_user``. False si no había invitación."""
    cur = db.execute(
        "UPDATE blend_links SET status = 'accepted' WHERE owner = ? AND partner = ? AND status = 'pending'",
        (from_user, user),
    )
    db.commit()
    return cur.rowcount > 0


def remove(db: Database, user: str, other: str) -> None:
    """Rompe el enlace (o rechaza / cancela la invitación) en ambos sentidos."""
    db.execute(
        "DELETE FROM blend_links WHERE (owner = ? AND partner = ?) OR (owner = ? AND partner = ?)",
        (user, other, other, user),
    )
    db.commit()


def is_linked(db: Database, a: str, b: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM blend_links WHERE status = 'accepted' AND "
        "((owner = ? AND partner = ?) OR (owner = ? AND partner = ?)) LIMIT 1",
        (a, b, b, a),
    ).fetchone()
    return row is not None


def partners(db: Database, user: str) -> list[str]:
    cur = db.execute(
        "SELECT partner FROM blend_links WHERE owner = ? AND status = 'accepted' "
        "UNION SELECT owner FROM blend_links WHERE partner = ? AND status = 'accepted'",
        (user, user),
    )
    return sorted(row[0] for row in cur.fetchall())


def incoming(db: Database, user: str) -> list[str]:
    """Invitaciones recibidas pendientes de aceptar."""
    cur = db.execute("SELECT owner FROM blend_links WHERE partner = ? AND status = 'pending' "
                     "ORDER BY created_at", (user,))
    return [row[0] for row in cur.fetchall()]


def outgoing(db: Database, user: str) -> list[str]:
    """Invitaciones enviadas que la otra persona aún no ha aceptado."""
    cur = db.execute("SELECT partner FROM blend_links WHERE owner = ? AND status = 'pending' "
                     "ORDER BY created_at", (user,))
    return [row[0] for row in cur.fetchall()]


def combine(mine: list[TopTrack], theirs: list[TopTrack], mode: str, limit: int) -> list[TopTrack]:
    """Une dos rankings.

    * comunes: solo las que están en los dos; primero las que más os gustan A LOS DOS
      (mayor mínimo de escuchas), luego por total. ``plays`` = suma de ambos.
    * mitad: alterna 1 tuya, 1 suya... sin repetir canciones.
    """
    if mode == "comunes":
        theirs_by_uri = {t.uri: t for t in theirs}
        shared = []
        for t in mine:
            other = theirs_by_uri.get(t.uri)
            if other is not None:
                shared.append((min(t.plays, other.plays), t.plays + other.plays,
                               TopTrack(t.uri, t.name, t.artist, t.plays + other.plays)))
        shared.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [track for _, _, track in shared[:limit]]
    if mode == "mitad":
        out: list[TopTrack] = []
        seen: set[str] = set()
        for i in range(max(len(mine), len(theirs))):
            for source in (mine, theirs):
                if i < len(source) and source[i].uri not in seen and len(out) < limit:
                    seen.add(source[i].uri)
                    out.append(source[i])
        return out
    raise ValueError(f"Modo de mezcla desconocido: {mode}")
