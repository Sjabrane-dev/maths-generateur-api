import sqlite3, sys
ordre = ["Repérage du plan","Nombres et ensembles de nombres","Arithmétique","Statistiques",
"Informations chiffrées et pourcentages","Probabilités","Fonctions : images et antécédents",
"Fonctions affines","Fonctions : équations et inéquations","Fonctions : variations et extrema",
"Fonctions paires et impaires","Fonctions de références","Géométrie des quadrilatères et des triangles",
"Vecteurs","Colinéarité de vecteurs","Applications de la colinéarité"]
c = sqlite3.connect(sys.argv[1])
anciens = [r[0] for r in c.execute("select titre from chapitres where classe='Seconde'")]
assert sorted(anciens) == sorted(ordre), "titres différents : " + str(set(anciens) ^ set(ordre))
c.execute("delete from chapitres where classe='Seconde'")
c.executemany("insert into chapitres values ('Seconde',?,?)", [(i+1, t) for i, t in enumerate(ordre)])
c.commit()
for r in c.execute("select numero,titre from chapitres where classe='Seconde' order by numero"): print(r)
