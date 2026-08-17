"""
DRMP Flask Backend — Multi-City District River Management Plan
Production-ready GIS API server. Supports every city present in the dataset.
"""
import os
import logging
from flask import Flask
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("drmp")


def create_app():
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024
    app.config["JSON_SORT_KEYS"] = False

    CORS(app, resources={r"/api/*": {"origins": os.getenv("CORS_ORIGINS", "*")}})

    from config import DATA_DIR
    if not DATA_DIR.exists():
        logger.error("=" * 70)
        logger.error("DATA_DIR does not exist: %s", DATA_DIR)
        logger.error("Every layer and DEM request will fail with FileNotFoundError.")
        logger.error("Set DATA_DIR in .env to the absolute path of your Input folder.")
        logger.error("=" * 70)
    else:
        logger.info("DATA_DIR: %s", DATA_DIR)

    from routes.layers   import layers_bp
    from routes.analysis import analysis_bp
    from routes.dem      import dem_bp
    from routes.reports  import reports_bp
    from routes.search   import search_bp
    from routes.cities   import cities_bp
    from routes.stp      import stp_bp

    app.register_blueprint(layers_bp,   url_prefix="/api/layers")
    app.register_blueprint(analysis_bp, url_prefix="/api/analysis")
    app.register_blueprint(dem_bp,      url_prefix="/api/dem")
    app.register_blueprint(reports_bp,  url_prefix="/api/reports")
    app.register_blueprint(search_bp,   url_prefix="/api/search")
    app.register_blueprint(cities_bp,   url_prefix="/api/cities")
    app.register_blueprint(stp_bp,      url_prefix="/api/stp")

    @app.route("/api/health")
    def health():
        return {"status": "ok", "version": "2.0.0", "project": "DRMP Multi-City"}

    @app.errorhandler(404)
    def not_found(e):
        return {"error": "Endpoint not found", "status": 404}, 404

    @app.errorhandler(500)
    def server_error(e):
        logger.error("Server error: %s", e)
        return {"error": "Internal server error", "status": 500}, 500

    logger.info("DRMP Flask app initialized (multi-city).")
    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)