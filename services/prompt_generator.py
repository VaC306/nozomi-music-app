from __future__ import annotations

from typing import Any


class PromptGeneratorError(Exception):
    """Raised when prompt input is invalid."""


class PromptGenerator:
    @staticmethod
    def list_prompt_types() -> list[dict[str, str]]:
        return [
            {
                "id": "discover",
                "title": "Descubrir musica",
                "description": "Para pedir nuevas canciones a partir de un genero, mood o referencia.",
            },
            {
                "id": "curate",
                "title": "Curar playlist",
                "description": "Para ordenar una playlist, definir energia y mejorar transiciones.",
            },
            {
                "id": "analyze",
                "title": "Analizar gustos",
                "description": "Para convertir artistas y canciones favoritas en insights musicales.",
            },
        ]

    def build_prompt(
        self,
        prompt_type: str,
        genre: str,
        mood: str,
        references: str,
        goal: str,
        constraints: str,
        output_language: str,
    ) -> dict[str, Any]:
        prompt_kind = prompt_type.strip().lower()
        if prompt_kind not in {"discover", "curate", "analyze"}:
            raise PromptGeneratorError("Selecciona un tipo de prompt valido.")

        goal_value = goal.strip()
        if not goal_value:
            raise PromptGeneratorError("Describe que quieres conseguir con el prompt.")

        genre_value = genre.strip() or "sin genero fijo"
        mood_value = mood.strip() or "sin mood definido"
        references_value = references.strip() or "sin referencias concretas"
        constraints_value = constraints.strip() or "sin restricciones especiales"
        language_value = output_language.strip() or "espanol"

        title_map = {
            "discover": "Prompt para descubrir musica",
            "curate": "Prompt para curar una playlist",
            "analyze": "Prompt para analizar gustos musicales",
        }

        instruction_map = {
            "discover": (
                "Actua como un curador musical experto en descubrimiento de canciones y escenas. "
                "Propone musica nueva y variada, evitando recomendaciones genericas."
            ),
            "curate": (
                "Actua como un editor musical senior especializado en ordenar playlists con narrativa, energia y transiciones coherentes."
            ),
            "analyze": (
                "Actua como un analista musical experto en detectar patrones de gusto, afinidades sonoras, epocas, escenas y perfiles de escucha."
            ),
        }

        task_map = {
            "discover": "Quiero descubrir musica nueva alineada con mis gustos.",
            "curate": "Quiero mejorar o redefinir una playlist existente.",
            "analyze": "Quiero entender mejor mis gustos y obtener conclusiones utiles.",
        }

        prompt_text = (
            f"{instruction_map[prompt_kind]}\n\n"
            f"Objetivo principal: {goal_value}.\n"
            f"Contexto de genero o escena: {genre_value}.\n"
            f"Mood o atmosfera deseada: {mood_value}.\n"
            f"Referencias base: {references_value}.\n"
            f"Restricciones o condiciones: {constraints_value}.\n"
            f"Idioma de salida: {language_value}.\n\n"
            f"Tarea: {task_map[prompt_kind]}\n\n"
            "Quiero una respuesta estructurada con estos puntos:\n"
            "1. Una interpretacion breve de lo que estoy buscando.\n"
            "2. Recomendaciones o conclusiones especificas, no genericas.\n"
            "3. Motivos claros de cada sugerencia o insight.\n"
            "4. Si aplica, una organizacion final lista para copiar y usar.\n"
            "5. Evita repetir artistas o ideas obvias si hay alternativas mas interesantes."
        )

        return {
            "title": title_map[prompt_kind],
            "type": prompt_kind,
            "prompt": prompt_text,
            "summary": {
                "genero": genre_value,
                "mood": mood_value,
                "referencias": references_value,
                "objetivo": goal_value,
                "restricciones": constraints_value,
                "idioma": language_value,
            },
        }
