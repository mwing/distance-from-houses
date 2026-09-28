from dataclasses import dataclass, field

HOUSE_TYPES = (
    "kerrostalo",
    "rivitalo",
    "paritalo",
    "erillistalo",
    "omakotitalo",
    "luhtitalo",
    "puutalo-osake",
)

PLOT_OWNERSHIP = ("own", "rent")

UUSIMAA_MUNICIPALITIES = frozenset(
    {
        "askola", "espoo", "hanko", "helsinki", "hyvinkää", "inkoo", "järvenpää",
        "karkkila", "kauniainen", "kerava", "kirkkonummi", "lapinjärvi", "lohja",
        "loviisa", "myrskylä", "mäntsälä", "nurmijärvi", "pornainen", "porvoo",
        "pukkila", "raasepori", "sipoo", "siuntio", "tuusula", "vantaa", "vihti",
    }
)

# Generous box around Uusimaa. Both sources are queried for Uusimaa only, so a listing
# outside it has bad coordinates.
UUSIMAA_BBOX = (59.7, 22.7, 60.95, 26.7)


@dataclass
class Filters:
    house_types: list[str] = field(default_factory=list)
    rooms: list[int] = field(default_factory=list)
    plot_ownership: list[str] = field(default_factory=list)
    price_min: int | None = None
    price_max: int | None = None
    size_min: float | None = None
    size_max: float | None = None
    build_year_min: int | None = None
    build_year_max: int | None = None
    municipalities: list[str] = field(default_factory=list)


@dataclass
class Listing:
    source: str
    id: str
    url: str
    address: str
    municipality: str
    lat: float | None
    lon: float | None
    price: float | None
    size: float | None
    rooms: int | None
    house_type: str | None
    build_year: int | None
    image: str | None = None
    other_urls: list[str] = field(default_factory=list)

    @property
    def in_uusimaa(self) -> bool:
        if self.lat is None or self.lon is None:
            return False
        south, west, north, east = UUSIMAA_BBOX
        return south <= self.lat <= north and west <= self.lon <= east


@dataclass
class Destination:
    name: str
    address: str | None = None
    lat: float | None = None
    lon: float | None = None
    modes: list[str] = field(default_factory=lambda: ["transit", "car"])
    weight: float = 1.0
    max_minutes: dict[str, float] = field(default_factory=dict)
    in_uusimaa: bool = False
