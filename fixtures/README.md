# Fixtures de prueba

Archivos pequeños para inspección manual. Convención de nombres: `<base>.<idioma>.json`.
Los resultados esperados están en `expected_results/` y las pruebas los comparan con la ejecución real.

## Caso `home` (origen en inglés → español y árabe)

| Archivo | Defecto deliberado | Resultado esperado |
|---|---|---|
| `home.es.json` | Falta `page.description` | error · clave_faltante |
| `home.es.json` | Sobra `page.promoBadge` | error · clave_inesperada |
| `home.es.json` | `page.itemCount` es `"3"` (cadena) en lugar de `3` | error · tipo_distinto |
| `home.es.json` | `{userName}` se tradujo a `{nombreUsuario}` | error · placeholder_alterado |
| `home.es.json` | `page.banner.legal` usa `\\n` literal en lugar de salto real | error · salto_linea_tipo_distinto |
| `home.es.json` | `_gpiInternalData-rootPath` difiere (`/es/`) | info · excepción EXC-002 (válida) |
| `home.es.json` | Título más largo, `brand` y `copyright` idénticos al inglés | info · diferencia legítima |
| `home.ar.json` | `_gpiInternalData-itemId` alterado | error · metadato_distinto (sin excepción) |
| `home.ar.json` | `footer.help` sin salto de línea | advertencia · cantidad de saltos distinta |
| `home.ar.json` | `page.links[1].label` vacío | advertencia · valor_vacio |
| `home.ar.json` | `toolName` y `rootPath` distintos | info · excepciones EXC-001 y EXC-002 |
| `home.ar.json` | El orden de `{userName}` cambia por RTL | sin hallazgo (el orden puede variar) |

## Caso limpio `clean` (origen → español)

`clean.en.json` → `clean.es.json` supera los controles (estado `PASS`). Solo contiene diferencias
legítimas: `rootPath` por idioma (excepción) y `brand` idéntico al inglés.

> Los textos de los valores JSON no se tradujeron a propósito: son datos de prueba bilingües.
