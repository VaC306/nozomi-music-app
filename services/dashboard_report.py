from __future__ import annotations

from datetime import datetime
from pathlib import Path

from flask import render_template

from services.playlist_manager import sanitize_filename
from services.stats_service import StatsSnapshot, TIME_RANGE_LABELS


class DashboardReportBuilder:
    def __init__(self, exports_dir: Path) -> None:
        self.exports_dir = exports_dir

    def build(self, snapshot: StatsSnapshot, selected_range: str) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_name = sanitize_filename(f"dashboard_{snapshot.profile.get('display_name', 'spotify')}")
        output_path = self.exports_dir / f"{report_name}_{timestamp}.html"
        html = render_template(
            "dashboard_export.html",
            snapshot=snapshot,
            selected_range=selected_range,
            time_range_labels=TIME_RANGE_LABELS,
        )
        output_path.write_text(html, encoding="utf-8")
        return output_path
