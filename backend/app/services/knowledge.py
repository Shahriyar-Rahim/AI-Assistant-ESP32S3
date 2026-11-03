"""Offline knowledge for the local brain: a bundled fact base, a safe calculator and unit converter, and a Wikipedia lookup.

The fact base is a small hand-written retrieval corpus, not a trained neural network; it only answers questions that are
close to an entry. Anything else goes to the cloud AI, then to the online lookup, then to an honest "I don't know".
"""

from __future__ import annotations

import ast
import logging
import math
import operator
import re
from urllib.parse import quote

import httpx

from app.services import local_brain
from app.services.speech_text import clean_for_speech

logger = logging.getLogger(__name__)

FACTS: tuple[tuple[str, str], ...] = (
    ("what is the capital of france", "The capital of France is Paris."),
    ("what is the capital of japan", "The capital of Japan is Tokyo."),
    ("what is the capital of germany", "The capital of Germany is Berlin."),
    ("what is the capital of italy", "The capital of Italy is Rome."),
    ("what is the capital of spain", "The capital of Spain is Madrid."),
    ("what is the capital of india", "The capital of India is New Delhi."),
    ("what is the capital of bangladesh", "The capital of Bangladesh is Dhaka."),
    ("what is the capital of china", "The capital of China is Beijing."),
    ("what is the capital of the united states", "The capital of the United States is Washington, D.C."),
    ("what is the capital of the united kingdom", "The capital of the United Kingdom is London."),
    ("what is the capital of canada", "The capital of Canada is Ottawa."),
    ("what is the capital of australia", "The capital of Australia is Canberra."),
    ("what is the capital of brazil", "The capital of Brazil is Brasilia."),
    ("what is the capital of russia", "The capital of Russia is Moscow."),
    ("what is the capital of egypt", "The capital of Egypt is Cairo."),
    ("what is the capital of pakistan", "The capital of Pakistan is Islamabad."),
    ("what is the capital of turkey", "The capital of Turkey is Ankara."),
    ("what is the capital of saudi arabia", "The capital of Saudi Arabia is Riyadh."),
    ("how many days are in a week", "There are seven days in a week."),
    ("how many days are in a year", "A year has 365 days, or 366 in a leap year."),
    ("how many months are in a year", "There are twelve months in a year."),
    ("how many hours are in a day", "There are 24 hours in a day."),
    ("how many minutes are in an hour", "There are 60 minutes in an hour."),
    ("how many seconds are in a minute", "There are 60 seconds in a minute."),
    ("how many continents are there", "There are seven continents: Asia, Africa, North America, South America, Antarctica, Europe and Australia."),
    ("how many oceans are there", "There are five oceans: Pacific, Atlantic, Indian, Southern and Arctic."),
    ("how many planets are in the solar system", "There are eight planets in the solar system."),
    ("what are the planets in the solar system", "Mercury, Venus, Earth, Mars, Jupiter, Saturn, Uranus and Neptune."),
    ("which planet is the largest", "Jupiter is the largest planet in the solar system."),
    ("which planet is closest to the sun", "Mercury is the closest planet to the sun."),
    ("which planet is known as the red planet", "Mars is known as the red planet."),
    ("how many legs does a spider have", "A spider has eight legs."),
    ("how many legs does an insect have", "An insect has six legs."),
    ("how many bones are in the human body", "An adult human has 206 bones."),
    ("how many players are on a football team", "A soccer team has eleven players on the field."),
    ("how many sides does a triangle have", "A triangle has three sides."),
    ("how many sides does a hexagon have", "A hexagon has six sides."),
    ("what is the largest ocean", "The Pacific Ocean is the largest ocean."),
    ("what is the largest country in the world", "Russia is the largest country by area."),
    ("what is the most populated country", "India and China are the two most populated countries, each with over 1.4 billion people."),
    ("what is the tallest mountain in the world", "Mount Everest is the tallest mountain above sea level, at about 8,849 meters."),
    ("what is the longest river in the world", "The Nile and the Amazon are the two longest rivers; the Nile is traditionally listed as longest."),
    ("what is the largest animal", "The blue whale is the largest animal ever known."),
    ("what is the fastest land animal", "The cheetah is the fastest land animal."),
    ("what is the speed of light", "Light travels at about 299,792 kilometers per second."),
    ("what is the speed of sound", "Sound travels at about 343 meters per second in air at room temperature."),
    ("what is the boiling point of water", "Water boils at 100 degrees Celsius, or 212 degrees Fahrenheit, at sea level."),
    ("what is the freezing point of water", "Water freezes at 0 degrees Celsius, or 32 degrees Fahrenheit."),
    ("what is the chemical symbol for water", "Water is H 2 O: two hydrogen atoms and one oxygen atom."),
    ("what is the chemical symbol for gold", "The chemical symbol for gold is A U."),
    ("what is the chemical symbol for salt", "Table salt is sodium chloride, written N a C l."),
    ("what gas do plants absorb", "Plants absorb carbon dioxide and release oxygen during photosynthesis."),
    ("what gas do humans breathe", "Humans breathe in oxygen and breathe out carbon dioxide."),
    ("what is photosynthesis", "Photosynthesis is how plants turn sunlight, water and carbon dioxide into sugar and oxygen."),
    ("what is gravity", "Gravity is the force that pulls objects with mass toward each other. On Earth it pulls things toward the ground."),
    ("what is the value of pi", "Pi is about 3.14159."),
    ("what is a prime number", "A prime number is a whole number greater than one that is divisible only by one and itself."),
    ("what is the square root of 144", "The square root of 144 is 12."),
    ("what is an atom", "An atom is the smallest unit of an element. It has a nucleus of protons and neutrons with electrons around it."),
    ("what is dna", "DNA is the molecule that carries genetic instructions for living things."),
    ("what is the sun", "The sun is the star at the center of our solar system. It is about 150 million kilometers from Earth."),
    ("what is the moon", "The moon is Earth's only natural satellite. It orbits Earth about every 27 days."),
    ("why is the sky blue", "The sky looks blue because air scatters short blue wavelengths of sunlight more than longer red ones."),
    ("why is the sea salty", "Rivers carry dissolved minerals from rocks into the sea, and when water evaporates the salt stays behind."),
    ("why do we sleep", "Sleep lets the body repair itself, and helps the brain store memories and clear waste."),
    ("what causes rain", "Water vapor rises, cools into clouds of tiny droplets, and when droplets grow heavy they fall as rain."),
    ("what causes the seasons", "Seasons happen because Earth's axis is tilted as it orbits the sun, changing how directly sunlight hits each hemisphere."),
    ("what is the internet", "The internet is a worldwide network of connected computers that share information using common protocols."),
    ("what is wi-fi", "Wi-Fi is a wireless way for devices to connect to a network using radio waves."),
    ("what is an esp32", "The ESP32 is a low-cost microcontroller from Espressif with built-in Wi-Fi and Bluetooth. I run on an ESP32 S3."),
    ("what is a microcontroller", "A microcontroller is a tiny computer on one chip that runs a single program and controls hardware."),
    ("what is artificial intelligence", "Artificial intelligence is software that performs tasks that normally need human intelligence, such as understanding speech or recognizing images."),
    ("what is machine learning", "Machine learning is a way for computers to learn patterns from data instead of following only fixed rules."),
    ("what is python", "Python is a popular, easy to read programming language used for web, data, automation and AI."),
    ("what is competitive programming", "Competitive programming is solving algorithm and math problems quickly in timed contests, using languages like C plus plus or Python."),
    ("what is an algorithm", "An algorithm is a step by step procedure for solving a problem."),
    ("what is a database", "A database is an organized collection of data that programs can store, search and update."),
    ("what is a leap year", "A leap year has an extra day, February 29, and happens every four years, with exceptions for some century years."),
    ("who invented the telephone", "Alexander Graham Bell is credited with inventing the telephone in 1876."),
    ("who invented the light bulb", "Thomas Edison developed the first practical light bulb, building on earlier work by others."),
    ("who discovered gravity", "Isaac Newton described the law of universal gravitation in the 1600s."),
    ("who wrote romeo and juliet", "William Shakespeare wrote Romeo and Juliet."),
    ("who painted the mona lisa", "Leonardo da Vinci painted the Mona Lisa."),
    ("who was the first person on the moon", "Neil Armstrong was the first person to walk on the moon, in 1969."),
    ("who is the founder of microsoft", "Bill Gates and Paul Allen founded Microsoft."),
    ("who is the founder of apple", "Steve Jobs, Steve Wozniak and Ronald Wayne founded Apple."),
    ("when did world war two end", "World War Two ended in 1945."),
    ("what language is spoken in brazil", "Portuguese is spoken in Brazil."),
    ("what language is spoken in bangladesh", "Bengali, also called Bangla, is the main language of Bangladesh."),
    ("what is the currency of japan", "The currency of Japan is the yen."),
    ("what is the currency of bangladesh", "The currency of Bangladesh is the taka."),
    ("what is the currency of india", "The currency of India is the rupee."),
    ("what is the currency of the united kingdom", "The currency of the United Kingdom is the pound sterling."),
    ("what are the primary colors", "The primary colors of light are red, green and blue. For paint they are traditionally red, yellow and blue."),
    ("how do i stay healthy", "Sleep seven to nine hours, eat plenty of vegetables, drink water, move daily and manage stress."),
    ("how much water should i drink", "A common guideline is about two liters of fluids a day, more if you are active or it is hot."),
    ("how do i boil an egg", "Place eggs in boiling water, cook about 7 minutes for soft or 10 to 12 for hard, then cool them in cold water."),
    ("how do i make tea", "Boil water, steep a tea bag for three to five minutes, then add milk or sugar if you like."),
    ("how do i relax", "Try slow breathing: breathe in for four seconds, hold for four, and out for six. Repeat a few times."),
    ("how do i focus better", "Remove distractions, work in 25 minute blocks with short breaks, and start with the smallest first step."),
    ("what are the days of the week", "Monday, Tuesday, Wednesday, Thursday, Friday, Saturday and Sunday."),
    ("what are the months of the year", "January, February, March, April, May, June, July, August, September, October, November and December."),
    ("tell me a fun fact", "Honey never spoils. Archaeologists have found edible honey in ancient Egyptian tombs."),
    ("tell me something interesting", "Octopuses have three hearts and blue blood."),
)

_FACT_INDEX = [(question, answer, local_brain.tokenize(question)) for question, answer in FACTS]
FACT_THRESHOLD = 0.72


def lookup_fact(prompt: str, threshold: float = FACT_THRESHOLD) -> str | None:
    tokens = local_brain.tokenize(prompt)
    if not tokens:
        return None
    best_score, best_answer = 0.0, None
    for question, answer, question_tokens in _FACT_INDEX:
        score = local_brain.similarity(prompt, question, tokens, question_tokens)
        if score > best_score:
            best_score, best_answer = score, answer
    return best_answer if best_score >= threshold else None


_OPERATORS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.Pow: operator.pow, ast.Mod: operator.mod, ast.USub: operator.neg, ast.UAdd: operator.pos,
}
_WORD_OPS = (
    (r"\bto the power of\b|\braised to\b", "**"), (r"\bsquared\b", "**2"), (r"\bcubed\b", "**3"),
    (r"\bmultiplied by\b|\btimes\b|\bx\b", "*"), (r"\bdivided by\b|\bover\b", "/"),
    (r"\bplus\b", "+"), (r"\bminus\b|\bsubtract(?:ed)?\b", "-"), (r"\bmodulo\b|\bmod\b", "%"),
)


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > 100 or abs(left) > 1e6):
            raise ValueError("exponent too large")
        return _OPERATORS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPERATORS:
        return _OPERATORS[type(node.op)](_eval(node.operand))
    raise ValueError("unsupported expression")


def _format_number(value: float) -> str:
    if math.isinf(value) or math.isnan(value):
        raise ValueError("not finite")
    if abs(value - round(value)) < 1e-9:
        return f"{int(round(value)):,}".replace(",", "")
    return f"{value:.6g}"


def calculate(prompt: str) -> str | None:
    text = prompt.casefold().replace(",", "")
    percent = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|percent)\s*of\s*(\d+(?:\.\d+)?)", text)
    if percent:
        return f"{_format_number(float(percent.group(1)) * float(percent.group(2)) / 100)}."
    root = re.search(r"square root of (\d+(?:\.\d+)?)", text)
    if root:
        return f"The square root of {root.group(1)} is {_format_number(math.sqrt(float(root.group(1))))}."
    if not re.search(r"\b(?:what(?:'s| is)|calculate|compute|how much is|equals?)\b|\d\s*[-+*/x]\s*\d", text):
        return None
    expression = re.sub(r"\b(?:what(?:'s| is)|calculate|compute|how much is|equals?|please|the|result of)\b", " ", text)
    for pattern, replacement in _WORD_OPS:
        expression = re.sub(pattern, f" {replacement} ", expression)
    expression = re.sub(r"[?!.]+$", "", expression.strip())
    if not re.fullmatch(r"[\d\s.+\-*/%()]+", expression) or not re.search(r"\d\s*[-+*/%]+\s*\(?-?\d", expression):
        return None
    try:
        value = _eval(ast.parse(expression.strip(), mode="eval"))
        return f"The answer is {_format_number(value)}."
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError, RecursionError):
        return None


_LENGTH = {"km": 1000.0, "kilometer": 1000.0, "kilometre": 1000.0, "m": 1.0, "meter": 1.0, "metre": 1.0, "cm": 0.01,
           "centimeter": 0.01, "mm": 0.001, "mile": 1609.344, "foot": 0.3048, "feet": 0.3048, "inch": 0.0254, "yard": 0.9144}
_MASS = {"kg": 1.0, "kilogram": 1.0, "g": 0.001, "gram": 0.001, "pound": 0.45359237, "lb": 0.45359237, "ounce": 0.0283495}
_UNIT = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*(kilometers?|kilometres?|km|meters?|metres?|cm|centimeters?|mm|miles?|feet|foot|inch(?:es)?|yards?|"
    r"kilograms?|kg|grams?|pounds?|lbs?|ounces?|celsius|fahrenheit|kelvin)\s*(?:to|in|into)\s*"
    r"(kilometers?|kilometres?|km|meters?|metres?|cm|centimeters?|mm|miles?|feet|foot|inch(?:es)?|yards?|"
    r"kilograms?|kg|grams?|pounds?|lbs?|ounces?|celsius|fahrenheit|kelvin)"
)


def _unit_key(name: str) -> str:
    name = name.rstrip("s") if name not in {"feet", "celsius"} else name
    return {"inche": "inch", "kilometre": "kilometer", "metre": "meter", "centimetre": "centimeter", "lb": "lb"}.get(name, name)


def convert_units(prompt: str) -> str | None:
    match = _UNIT.search(prompt.casefold())
    if not match:
        return None
    amount, source, target = float(match.group(1)), _unit_key(match.group(2)), _unit_key(match.group(3))
    temperature = {"celsius", "fahrenheit", "kelvin"}
    if source in temperature and target in temperature:
        celsius = amount if source == "celsius" else (amount - 32) * 5 / 9 if source == "fahrenheit" else amount - 273.15
        result = celsius if target == "celsius" else celsius * 9 / 5 + 32 if target == "fahrenheit" else celsius + 273.15
    elif source in _LENGTH and target in _LENGTH:
        result = amount * _LENGTH[source] / _LENGTH[target]
    elif source in _MASS and target in _MASS:
        result = amount * _MASS[source] / _MASS[target]
    else:
        return None
    return f"{_format_number(amount)} {match.group(2)} is about {round(result, 2):g} {match.group(3)}."


WIKI_API = "https://en.wikipedia.org/w/api.php"
WEB_TIMEOUT_SECONDS = 6.0
WIKI_USER_AGENT = (
    "BuddyAssistant/1.0 (https://github.com/Shahriyar-Rahim/AI-Assistant-ESP32S3; personal voice assistant) python-httpx"
)
_QUESTION_WORDS = re.compile(
    r"^(?:what(?:'s| is| are| was| were)|who(?:'s| is| was| are)|tell me about|explain|define|describe|where is|when was|"
    r"how does|how do|why is|why does|why do)\s+(?:the |a |an )?", re.I)
_OPINION = re.compile(r"\b(?:future|opinion|should i|best|better|will .* replace|predict|recommend)\b", re.I)


async def web_answer(prompt: str) -> str | None:
    """Looks the topic up on Wikipedia (official API). Returns plain spoken text or None."""
    topic = _QUESTION_WORDS.sub("", prompt.strip().rstrip("?.! ")).strip()
    topic = re.sub(r"\b(?:please|for me|hey buddy)\b", "", topic, flags=re.I).strip()
    if len(topic) < 3 or _OPINION.search(prompt):
        return None
    wanted = set(local_brain.tokenize(topic))
    if not wanted:
        return None
    try:
        async with httpx.AsyncClient(timeout=WEB_TIMEOUT_SECONDS, headers={"User-Agent": WIKI_USER_AGENT}) as client:
            response = await client.get(WIKI_API, params={
                "action": "query", "format": "json", "generator": "search", "gsrsearch": topic, "gsrlimit": 3,
                "prop": "extracts", "exintro": 1, "explaintext": 1, "exsentences": 3, "redirects": 1,
            })
            response.raise_for_status()
            pages = list((response.json().get("query") or {}).get("pages", {}).values())
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("Online lookup unavailable: %s", exc)
        return None
    pages.sort(key=lambda page: page.get("index", 99))
    for page in pages:
        title_tokens = set(local_brain.tokenize(str(page.get("title", ""))))
        extract = str(page.get("extract") or "").strip()
        # Require the page title to share a content word with the question, so unrelated hits are never read out.
        if extract and title_tokens & wanted:
            extract = re.sub(r"\s*\([^()]*\)", "", extract)  # drops pronunciations and dates in brackets
            spoken = clean_for_speech(extract, limit=420)
            if spoken:
                return spoken
    return None
