"""Install metadata for the root-hosted dashboard, independent of telemetry."""
from fastapi.responses import FileResponse
from nicegui import app

from solar_model.paths import package_path


INSTALL_HEAD_HTML = '''
<link rel="manifest" href="/manifest.webmanifest" crossorigin="use-credentials">
<meta name="theme-color" content="#ffffff">
<meta name="mobile-web-app-capable" content="yes">
'''


def register_installation_assets() -> None:
    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest() -> FileResponse:
        return FileResponse(package_path("dashboard/assets/manifest.webmanifest"), media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})

    for size in (192, 512):
        app.add_static_file(local_file=package_path(f"dashboard/assets/solar-app-{size}.png"), url_path=f"/solar-app-{size}.png")
