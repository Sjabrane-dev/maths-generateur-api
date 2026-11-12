# Fichier : API_RENDER/main.py
import os
import shutil
import sqlite3
from contextlib import closing

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from generateur import assembler_et_compiler, ErreurGeneration, MAX_EXERCICES
from config import CHEMIN_BASE
from editeur_latex import router as routeur_editeur
from entrainement import router as routeur_entrainement

app = FastAPI()

# Sur Render, définir la variable d'environnement ORIGINES_AUTORISEES avec
# l'adresse de votre site, par ex. : https://mon-site.fr
# (plusieurs adresses possibles, séparées par des virgules).
ORIGINES = [o.strip() for o in os.environ.get("ORIGINES_AUTORISEES", "*").split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGINES,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class DemandeFiche(BaseModel):
    exercices: list[str] = Field(min_length=1, max_length=MAX_EXERCICES)
    titre: str | None = Field(default="Feuille d'exercices", max_length=120)
    classe: str | None = Field(default="", max_length=60)
    etablissement: str | None = Field(default="", max_length=120)
    duree: str | None = Field(default="", max_length=40)   # facultatif (ex. « 1 h » pour un contrôle)
    # barème facultatif : {"EXP-012": 4, "EXP-013": 2.5} (points par exercice)
    points: dict[str, float] | None = None   # (pas de max_length : non géré par pydantic v1)
    # page de présentation facultative (première page de garde)
    page_presentation: bool = False
    style_presentation: str | None = Field(default="moderne", max_length=20)  # "moderne" ou "chapitre"
    citation: str | None = Field(default="", max_length=300)
    auteur_citation: str | None = Field(default="", max_length=80)
    # consigne facultative : "" (rien), "autorisee" ou "interdite"
    calculatrice: str | None = Field(default="", max_length=20)
    # corrigés : "sans" (énoncés seuls), "fin" (énoncés puis corrigés), "seuls" (corrigés seuls)
    corrections: str | None = Field(default="sans", max_length=10)
    # devoirs A/B : "A" (une seule version) ou "AB" (sujet B tiré dans la banque : même chapitre, même difficulté)
    versions: str | None = Field(default="A", max_length=2)
    variante: int = Field(default=0, ge=0, le=999)   # autre tirage du sujet B


@app.get("/")
def accueil():
    return {"statut": "ok"}


# Fonctions « def » (et non « async def ») : FastAPI les exécute dans un fil
# séparé, donc une compilation LaTeX ne bloque plus tout le serveur.
@app.get("/exercices")
def liste_exercices():
    with closing(sqlite3.connect(CHEMIN_BASE)) as conn:
        resultats = conn.execute("""
            SELECT id, titre, niveau, chapitre, difficulte, enonce, correction
            FROM exercices
            ORDER BY niveau, chapitre, id
        """).fetchall()

    return [
        {
            "id": l[0], "titre": l[1], "niveau": l[2], "chapitre": l[3],
            "difficulte": l[4], "enonce": l[5], "correction": l[6]
        }
        for l in resultats
    ]


@app.get("/chapitres/{niveau}")
def liste_chapitres(niveau: str):
    with closing(sqlite3.connect(CHEMIN_BASE)) as conn:
        resultats = conn.execute(
            "SELECT numero, titre FROM chapitres WHERE classe = ? ORDER BY numero", (niveau,)
        ).fetchall()
    return [{"numero": l[0], "titre": l[1]} for l in resultats]


@app.post("/generer-pdf")
def api_generer_pdf(demande: DemandeFiche):
    try:
        chemin_pdf, dossier = assembler_et_compiler(
            liste_choix=demande.exercices,
            titre_fiche=demande.titre or "",
            classe_fiche=demande.classe or "",
            etablissement_fiche=demande.etablissement or "",
            duree_fiche=demande.duree or "",
            points=demande.points or {},
            page_presentation=demande.page_presentation,
            style_presentation=demande.style_presentation or "moderne",
            citation=demande.citation or "",
            auteur_citation=demande.auteur_citation or "",
            calculatrice=demande.calculatrice or "",
            corrections=demande.corrections or "sans",
            versions=demande.versions or "A",
            variante=demande.variante or 0,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ErreurGeneration as e:
        raise HTTPException(status_code=500, detail=str(e))

    # le dossier temporaire est supprimé une fois le PDF envoyé
    return FileResponse(
        chemin_pdf,
        media_type="application/pdf",
        filename="votre_fiche.pdf",
        background=BackgroundTask(shutil.rmtree, dossier, ignore_errors=True),
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
app.include_router(routeur_editeur)
app.include_router(routeur_entrainement)
