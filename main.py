# Fichier : main.py
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import os
import sqlite3

# On importe la fonction de compilation depuis votre autre fichier
from generateur import assembler_et_compiler

app = FastAPI()

# Permet à votre site local de communiquer avec votre API
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5500",
        "http://localhost:5500",
        "https://maths-brel.netlify.app"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class DemandeFiche(BaseModel):
    exercices: list[str]
    titre: str = "Feuille d'exercices"
    classe: str = ""
    etablissement: str = ""

# --- ROUTE 1 : Liste de tous les exercices ---
@app.get("/exercices")
async def liste_exercices():
    conn = sqlite3.connect("banque.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, titre, niveau, chapitre, difficulte, enonce, correction 
        FROM exercices 
        ORDER BY niveau, chapitre
    """)
    resultats = cursor.fetchall()
    conn.close()
    
    return [
        {
            "id": r[0],
            "titre": r[1],
            "niveau": r[2],
            "chapitre": r[3],
            "difficulte": r[4],
            "enonce": r[5],
            "correction": r[6]
        }
        for r in resultats
    ]

# --- ROUTE 2 : Liste des chapitres pour un niveau donné ---
@app.get("/chapitres/{niveau}")
async def liste_chapitres(niveau: str):
    conn = sqlite3.connect("banque.db")
    cursor = conn.cursor()
    cursor.execute("SELECT numero, titre FROM chapitres WHERE classe = ? ORDER BY numero", (niveau,))
    resultats = cursor.fetchall()
    conn.close()
    
    return [
        {"numero": r[0], "titre": r[1]}
        for r in resultats
    ]

# --- ROUTE 3 : Générer le PDF ---
@app.post("/generer-pdf")
async def api_generer_pdf(demande: DemandeFiche):
    if not demande.exercices:
        raise HTTPException(status_code=400, detail="La liste d'exercices est vide.")
    
    try:
        # On passe les 4 arguments à la fonction de compilation
        assembler_et_compiler(
            liste_choix=demande.exercices,
            titre_personnalise=demande.titre,
            classe_personnalisee=demande.classe,
            etablissement=demande.etablissement
        )
        
        chemin_pdf = "feuille_exercices.pdf"
        
        if os.path.exists(chemin_pdf):
            nom_fichier_propre = demande.titre.replace(" ", "_").replace("'", "") + ".pdf"
            return FileResponse(
                chemin_pdf, 
                media_type="application/pdf", 
                filename=nom_fichier_propre
            )
        else:
            raise HTTPException(status_code=500, detail="Le PDF n'a pas pu être généré.")
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)