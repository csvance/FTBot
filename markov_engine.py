import json
import zlib
from enum import unique, Enum
from typing import Optional, List
import re
import numpy as np
from spacy.tokens import Doc, Span, Token

from ml_config import MARKOV_WINDOW_SIZE, MARKOV_GENERATION_WEIGHT_COUNT, MARKOV_GENERATION_WEIGHT_RATING, MARKOV_GENERATE_SUBJECT_POS_PRIORITY
from nlp_common import PosEnum, get_pos_from_token, one_hot


class MatrixIdx(object):
    pass


class WordKey(object):
    TEXT = '_T'
    POS = '_P'


class NeighborIdx(object):
    POS = 0
    VALUE_MATRIX = 1
    DISTANCE_MATRIX = 2


@unique
class NeighborValueIdx(Enum):
    COUNT = 0
    RATING = 1


class MarkovNeighbor(object):
    def __init__(self, text: str, pos: PosEnum, values: list, dist: list):
        self.text = text
        self.pos = pos
        self.values = values
        self.dist = dist

    def __repr__(self):
        return self.text

    @staticmethod
    def from_token(token: Token) -> 'MarkovNeighbor':
        text = token.text
        pos = get_pos_from_token(token)
        values = [0, 0]
        dist = [0] * (MARKOV_WINDOW_SIZE * 2 + 1)
        return MarkovNeighbor(text, pos, values, dist)

    @staticmethod
    def from_db_format(key: str, val: list) -> 'MarkovNeighbor':
        text = key
        pos = val[NeighborIdx.POS]
        values = val[NeighborIdx.VALUE_MATRIX]
        dist = val[NeighborIdx.DISTANCE_MATRIX]
        return MarkovNeighbor(text, pos, values, dist)

    def to_db_format(self) -> tuple:
        return self.text, [self.pos.value, self.values, self.dist]

    @staticmethod
    def distance_one_hot(dist):
        return one_hot(dist + MARKOV_WINDOW_SIZE, MARKOV_WINDOW_SIZE * 2 + 1)


class MarkovNeighbors(object):
    def __init__(self, neighbors: List[MarkovNeighbor]):
        self.neighbors = neighbors

    def __iter__(self) -> MarkovNeighbor:
        for neighbor in self.neighbors:
            yield neighbor

    def __len__(self):
        return len(self.neighbors)

    def __getitem__(self, item):
        return self.neighbors[item]


@unique
class ProjectionDirection(Enum):
    LEFT = 1
    RIGHT = 2


class MarkovWordProjection(object):
    def __init__(self, magnitudes: np.ndarray, distances: np.ndarray, keys: List[str], pos: List[PosEnum]):
        self.magnitudes = magnitudes
        self.distances = distances
        self.keys = keys
        self.pos = pos

    def __len__(self):
        return len(self.keys)


class MarkovWordProjectionCollection(object):
    def __init__(self, projections: List[MarkovWordProjection]):
        self.magnitudes = None
        self.distances = None
        self.keys = []
        self.pos = []

        self._concat_collection(projections)

    def _concat_collection(self, projections: List[MarkovWordProjection]):
        for projection_idx, projection in enumerate(projections):

            self.keys += projection.keys
            self.pos += projection.pos

            if projection_idx == 0:
                self.magnitudes = projection.magnitudes
                self.distances = projection.distances
            else:
                self.magnitudes = np.concatenate((self.magnitudes, projection.magnitudes))
                self.distances = np.concatenate((self.distances, projection.distances))

    def probability_matrix(self) -> np.ndarray:

        distance_magnitudes = self.distances * self.magnitudes
        sums = np.sum(distance_magnitudes, axis=0)
        p_values = distance_magnitudes / sums

        return p_values

    def __len__(self):
        return len(self.keys)


class MarkovWord(object):
    def __init__(self, text: str, pos: PosEnum, neighbors: dict):
        self.text = text
        self.pos = pos
        self.neighbors = neighbors

    def __repr__(self):
        return self.text

    def clear_cache(self):
        self._project_cache = {}

    def to_db_format(self) -> tuple:
        return {WordKey.TEXT: self.text, WordKey.POS: self.pos.value}, {MarkovTrieDb.NEIGHBORS_KEY: self.neighbors}

    @staticmethod
    def from_db_format(row: dict) -> 'MarkovWord':
        word = MarkovWord(row[MarkovTrieDb.WORD_KEY][WordKey.TEXT],
                          PosEnum(row[MarkovTrieDb.WORD_KEY][WordKey.POS]),
                          row[MarkovTrieDb.NEIGHBORS_KEY])
        return word

    @staticmethod
    def from_token(token: Token) -> 'MarkovWord':
        return MarkovWord(token.text, get_pos_from_token(token), {})

    def get_neighbor(self, word) -> Optional[MarkovNeighbor]:
        if word in self.neighbors:
            n_row = self.neighbors[word]
            return MarkovNeighbor(word, PosEnum(n_row[NeighborIdx.POS]), n_row[NeighborIdx.VALUE_MATRIX],
                                  n_row[NeighborIdx.DISTANCE_MATRIX])
        return None

    def set_neighbor(self, neighbor: MarkovNeighbor):
        n_row = [None, None, None]
        n_row[NeighborIdx.POS] = neighbor.pos.value
        n_row[NeighborIdx.VALUE_MATRIX] = neighbor.values
        n_row[NeighborIdx.DISTANCE_MATRIX] = neighbor.dist
        self.neighbors[neighbor.text] = n_row

    def select_neighbors(self, pos: PosEnum) -> MarkovNeighbors:
        results = []
        for key in self.neighbors:
            neighbor = self.get_neighbor(key)
            if neighbor.pos == pos:
                results.append(neighbor)

        return MarkovNeighbors(results)

    def project(self, idx_in_sentence: int, sentence_length: int, pos: PosEnum) -> MarkovWordProjection:

        # Get all neighbors
        neighbors = self.select_neighbors(pos)

        neighbor_keys = []
        neighbor_pos = []

        # Setup matrices
        distance_distributions = np.zeros((len(neighbors), sentence_length))
        neighbor_magnitudes = np.zeros((len(neighbors), 1))

        for neighbor_idx, neighbor in enumerate(neighbors):

            # Save Key
            neighbor_keys.append(neighbor.text)
            neighbor_pos.append(neighbor.pos)

            # Project dist values onto matrix space
            for dist_idx, dist_value in enumerate(neighbor.dist):

                # The actual index of this dist value within our matrix space
                dist_space_index = (dist_idx - MARKOV_WINDOW_SIZE) + idx_in_sentence

                # Bounds check
                if not (dist_space_index >= 0 and dist_space_index < sentence_length):
                    continue

                distance_distributions[neighbor_idx][dist_space_index] = dist_value

            # Calculate strength
            neighbor_magnitudes[neighbor_idx] = neighbor.values[NeighborValueIdx.COUNT.value] * MARKOV_GENERATION_WEIGHT_COUNT \
                                           + \
                                           neighbor.values[NeighborValueIdx.RATING.value] * MARKOV_GENERATION_WEIGHT_RATING

        return MarkovWordProjection(neighbor_magnitudes, distance_distributions, neighbor_keys, neighbor_pos)


class MarkovTrieDb(object):
    WORD_KEY = '_W'
    NEIGHBORS_KEY = '_N'

    def __init__(self, path: str = None):
        self._trie = {}
        if path is not None:
            self.load(path)

    def load(self, path: str):
        data = zlib.decompress(open(path, 'rb').read()).decode()
        self._trie = json.loads(data)

    def save(self, path: str):
        data = zlib.compress(json.dumps(self._trie, separators=(',', ':')).encode())
        f = open(path, 'wb').write(data)

    def _getnode(self, word: str) -> Optional[dict]:
        if len(word) == 0:
            return None

        node = self._trie
        for c in word:
            try:
                node = node[c.lower()]
            except KeyError:
                return None

        return node

    def _select(self, word: str) -> Optional[dict]:
        node = self._getnode(word)
        if node is None:
            return None

        if MarkovTrieDb.WORD_KEY in node:
            return node
        else:
            return None

    def select(self, word: str) -> MarkovWord:
        row = self._select(word)
        return MarkovWord.from_db_format(row) if row is not None else None

    def _insert(self, word: str, pos: int, neighbors: dict) -> Optional[dict]:

        node = self._trie
        for c in word:
            if c.lower() in node:
                node = node[c.lower()]
            else:
                node[c.lower()] = {}
                node = node[c.lower()]

        node[MarkovTrieDb.WORD_KEY] = {WordKey.TEXT: word, WordKey.POS: pos}
        node[MarkovTrieDb.NEIGHBORS_KEY] = neighbors
        return node

    def insert(self, word: MarkovWord) -> MarkovWord:
        row = self._insert(word.text, word.pos.value, word.neighbors)
        return MarkovWord.from_db_format(row) if row is not None else None

    def _update(self, word: str, pos: int, neighbors: dict) -> Optional[dict]:
        node = self._select(word)
        if node is None:
            return None

        node[MarkovTrieDb.WORD_KEY] = {WordKey.TEXT: word, WordKey.POS: pos}
        node[MarkovTrieDb.NEIGHBORS_KEY] = neighbors
        return node

    def update(self, word: MarkovWord) -> Optional[MarkovWord]:
        node = self._update(word.text, word.pos.value, word.neighbors)
        return MarkovWord.from_db_format(node) if node is not None else None


class MarkovGenerator(object):
    def __init__(self, structure: List[PosEnum], subjects: List[MarkovWord]):
        self.structure = structure
        self.subjects = subjects

        self.sentence_generations = []
        self.sentence_structures = []

    def _sort_subjects(self):

        sorted_subjects = []
        for subject_priority in MARKOV_GENERATE_SUBJECT_POS_PRIORITY:
            for subject in self.subjects:
                if subject.pos == subject_priority:
                    sorted_subjects.append(subject)
        self.subjects = sorted_subjects

    def generate(self, db: MarkovTrieDb) -> Optional[List[List[MarkovWord]]]:

        self._split_sentences()
        self._sort_subjects()
        if not self._assign_subjects():
            return None
        if not self._generate_words(db):
            return None

        return self.sentence_generations

    # Split into individual sentences and populate generation arrays
    def _split_sentences(self):
        start_index = 0
        for pos_idx, pos in enumerate(self.structure):
            if pos == PosEnum.EOS:
                # Separate structures into sentences
                sentence = self.structure[start_index:pos_idx]
                self.sentence_structures.append(sentence)

                # Create unfilled arrays for each sentence to populate later
                generates = [None] * len(sentence)
                self.sentence_generations.append(generates)

                start_index = pos_idx + 1

    # Assign one subject to each sentence with descending priority
    def _assign_subjects(self) -> bool:

        sentences_assigned = [None] * len(self.sentence_structures)

        for sentence_idx, sentence in enumerate(self.sentence_structures):
            for word_idx, pos in enumerate(sentence):
                for subject in self.subjects:
                    if subject.pos == pos:
                        self.sentence_generations[sentence_idx][word_idx] = subject
                        sentences_assigned[sentence_idx] = True
                        break

        # Each sentence should be assigned one subject to begin with
        for sentence in sentences_assigned:
            if sentence is False:
                return False

        return True

    def _work_remaining(self):
        work_left = 0
        for sentence_idx, sentence in enumerate(self.sentence_generations):
            for word_idx, word in enumerate(sentence):
                if word is None:
                    work_left += 1
        return work_left

    def _generate_words(self, db: MarkovTrieDb):

        old_work_left = self._work_remaining()
        while True:

            for sentence_idx, sentence in enumerate(self.sentence_generations):

                sentence_length = len(sentence)

                def handle_projections():
                    if blank_idx is not None and len(project_idx) > 0:
                        projections = []
                        blank_pos = self.sentence_structures[sentence_idx][blank_idx]
                        for word_idx in project_idx:
                            projecting_word = self.sentence_generations[sentence_idx][word_idx]
                            projection = projecting_word.project(word_idx, sentence_length, blank_pos)
                            projections.append(projection)

                        # Concatenate all projections and create p-value matrix
                        projection_collection = MarkovWordProjectionCollection(projections)
                        if len(projection_collection) == 0:
                            return False

                        all_p_values = projection_collection.probability_matrix()

                        # We just want the p-values for the blank word
                        p_values = all_p_values[:, blank_idx]

                        # Choose an index based on the probability
                        choices = np.arange(len(projection_collection))
                        word_choice_idx = np.random.choice(choices, p=p_values)

                        # Select the word from the database and assign it to the blank space
                        select_word = projection_collection.keys[word_choice_idx]
                        word = db.select(select_word)
                        self.sentence_generations[sentence_idx][blank_idx] = word

                # Work right to left
                blank_idx = None
                project_idx = []
                for word_idx, word in enumerate(sentence):
                    if word is None:
                        blank_idx = word_idx
                    elif blank_idx is not None and abs(blank_idx - word_idx) <= MARKOV_WINDOW_SIZE:
                        project_idx.append(word_idx)
                        break
                handle_projections()

                # Work left to right
                blank_idx = None
                project_idx = []
                for word_idx, word in enumerate(reversed(sentence)):
                    word_idx = (len(sentence)-1) - word_idx
                    if word is None:
                        blank_idx = word_idx
                    elif blank_idx is not None and abs(blank_idx - word_idx) <= MARKOV_WINDOW_SIZE:
                        project_idx.append(word_idx)
                        break
                handle_projections()

            # Check if we accomplished any work
            new_work_left = self._work_remaining()
            if old_work_left == new_work_left:
                return False
            elif new_work_left == 0:
                return True
            old_work_left = new_work_left


class MarkovFilters(object):
    @staticmethod
    def filter_input(text: str):
        if text is None:
            return None
        filtered = text

        filtered = re.sub(r'(&amp;)', '', filtered)
        filtered = re.sub(r'[,:;\'`\-_“^"(){}/\\*]', '', filtered)

        return filtered

    @staticmethod
    def smooth_output(text: str):
        if text is None:
            return None
        smoothed = text
        smoothed = re.sub(r' ([.,?!%])', r'\1', smoothed)
        return smoothed

class MarkovTrainer(object):
    def __init__(self, engine: MarkovTrieDb):
        self.engine = engine

    def learn(self, doc: Doc):
        ngrams = []
        for sentence in doc.sents:
            ngrams += MarkovTrainer.ngramify(sentence)

        row_cache = {}
        for ngram in ngrams:
            if ngram[0].text in row_cache:
                word = row_cache[ngram[0].text]
            else:
                # Attempt to load from DB
                word = self.engine.select(ngram[0].text)
                if word is None:
                    # If not already in the DB, create a new word object
                    word = MarkovWord.from_token(ngram[0])

            # Handle neighbor
            if ngram[1].text in word.neighbors:
                neighbor = word.get_neighbor(ngram[1].text)
            else:
                neighbor = MarkovNeighbor.from_token(ngram[1])

            # Increase Count
            neighbor.values[NeighborValueIdx.COUNT.value] += 1

            # Add distance
            dist_one_hot_base = np.array(neighbor.dist)
            dist_one_hot_add = np.array(MarkovNeighbor.distance_one_hot(ngram[2]))

            neighbor.dist = (dist_one_hot_base + dist_one_hot_add).tolist()

            # Convert to db format and store in word
            key, neighbor_db = neighbor.to_db_format()
            word.neighbors[key] = neighbor_db

            # Write word to DB
            if self.engine.update(word) is None:
                self.engine.insert(word)

            # Cache word
            row_cache[ngram[0].text] = word

    @staticmethod
    def ngramify(span: Span) -> list:

        grams = []

        for a_idx, a in enumerate(span):
            for b_idx, b in enumerate(span):

                dist = b_idx - a_idx
                if dist == 0:
                    continue

                elif abs(dist) <= MARKOV_WINDOW_SIZE:
                    grams.append([a, b, dist])

        return grams


