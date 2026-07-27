# GEE Dataset Intelligence Skill

Search, filter, compare, recommend, and explain Google Earth Engine public datasets using a bundled bilingual official catalog.

## Features

- **Local offline queries** — bundled catalog index for fast dataset lookup
- **Bilingual support** — English and Chinese dataset descriptions
- **Smart filtering** — by region, resolution, data type, availability
- **Official sources** — includes Google Earth Engine documentation URLs and citations
- **Catalog refresh** — explicit update command to fetch latest metadata

## Installation

```bash
# Clone the repository
git clone https://github.com/ruiduobao/gee-dataset-intelligence-skill.git

# Install dependencies (if any)
pip install -r requirements.txt
```

## Usage

```bash
# Query the catalog
python scripts/query_catalog.py --help

# Update the catalog
python scripts/update_catalog.py
```

## Use Cases

- Dataset selection for Earth Engine projects
- Finding dataset IDs, bands, and resolution info
- Understanding coverage, licensing, and citation requirements
- Comparing similar datasets for a specific region

## License

See [LICENSE](LICENSE) for details.
