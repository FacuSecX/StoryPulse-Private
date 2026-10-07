#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Acceso compatible al generador de sesiones de StoryPulse v2.0."""
from crear_session import main

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit("Inicio de sesión cancelado.")
    except RuntimeError as error:
        raise SystemExit(f"❌ {error}")
