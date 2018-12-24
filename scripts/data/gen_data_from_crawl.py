"""
generates a TSV parallel corpus from a crawl (the output of gain_wiki_revision.py)

python gen_data_from_crawl.py wiki_crawl/final_data.pkl CACHE OUT

pickle_path = sys.argv[1]
cache_path = sys.argv[2]
out_prefix = sys.argv[3]


ONLY TAKE REVISIONS WITH 1 CHANGED SENTENCE?

TODO REMAKDE DATA FROM THE BEGINNING, MAKE SURE TAGS ARE GETTING IN THERE SO YOU CAN DELETE THEM,
        THEN FIDDLE WITH THE LENGTH THRESHOLD!!

# IGNORE IF ONLY URL IS CHANGED, E.G. https://en.wikipedia.org/w/index.php?diff=9779941 (before sent tokenization)

# BETTER URL STRIPPING: http://lcweb2.loc.gov/cgi-bin/query/r?frd/cstdy:@field(DOCID+iq0023
    use "http\S+" ??



the british isles are a group of islands off the northwest coast of continental europe consisting of great britain, ireland, and a number of smaller surrounding islands and islets. "british isles," encyclopaedia britannica. the term "british isles" can be confusing ( see british isles (terminology) ) and is objectionable to some people in ireland.<ref name="myers"> an irishman's diary myers , <del class="diffchange diffchange-inline"> kevin; the irish times (subscription needed) 09/03/2000, accessed july 2006 'millions of people from these islands - oh how angry we get when people call them the british isles' </ref> see </del> the terminology section below for details of the controversy .''
although there is sparse knowledge about the avar language, scholars generally posit that the extinct language of the eurasian avars belonged to the subgroup ,price, glanville. encyclopedia of the languages of europe (2000) p 68.marcantonio, angela. the uralic language family (2002) p 24.rna-tas, andrs. hungarians and europe in the early middle ages (1999) p 116. and the language itself is referred to as ''' turkic avar''' in order to distinguish it from the spoken by the modern .<ref>e. j. '''' (2002) p <del class="diffchange diffchange-inline"> 127.</ref><ref>for </del> references on the classification of turkic avar , see the main article <del class="diffchange diffchange-inline"> on oghur languages.</ref> based on the reports </del> of , a greek historian, some scholars affirm a linguistic connection between the terms hungarus and onogur . angela marcantonio, the uralic language family: facts, myths and statistics, wiley-blackwell, 2002, p . <del class="diffchange diffchange-inline"> 24 </del> however, it is generally accepted that at least the leading stratum of the avars spoke a chuvash-type turkic language.<ref>andrs rna-tas, hungarians and europe in the early middle ages , central european university press, 1999, pp.115 </ref>
hubbard coined the name dianetics from the greek stems dia, meaning "through," and nous, meaning "mind," resulting in a word similar to the already-existing greek adjective dianotik-os -, meaning "mental" . the suffix "-etics" appears to have been inspired by cybernetics , a vogue idea at the time ( indeed, hubbard explicitly made this connection in a 1949 magazine article <ref> hubbard, "terra incognita: the mind", the explorers journal, winter 1949 / spring 1950 < / ref>) .


"""
import sys
import os
import pickle
from itertools import groupby
import random
import mwparserfromhell
import re
from nltk import sent_tokenize, word_tokenize
import Levenshtein
import numpy as np
from collections import Counter
import math
from tqdm import tqdm

from nltk import sent_tokenize, word_tokenize

from pytorch_pretrained_bert.tokenization import BertTokenizer
from simplediff import diff
from spellchecker import SpellChecker
from autocorrect import spell



crawl_path = sys.argv[1]
cache_path = sys.argv[2]
out_prefix = sys.argv[3]


CTR_EMPTY_REV = 0
CTR_MULTIPLE_EDITS = 0
CTR_FAILED_CLEANING = 0
CTR_LOW_BLEU = 0
CTR_LOW_LEVEN = 0
CTR_TOO_MANY_1_TOKS = 0
CTR_SPELLING = 0
CTR_FALSE_POSITIVE = 0
CTR_LENGTH_RATIO = 0

BERT_MODEL = "bert-base-uncased"
TOKENIZER = BertTokenizer.from_pretrained(BERT_MODEL, cache_dir=cache_path)





def rm_refs(x):
    REF_RE = '<ref([-\w=" <>]+)?>.*?<([ ]+)?\/([ ]+)?ref>'
    x = re.sub(REF_RE, ' ', x)
    # leading </ref>
    if '</ref>' in x:
        x = re.sub(REF_RE, ' ', '<ref>' + x)
    # trailing <ref>
    if '<ref' in x:
        x = re.sub(REF_RE, ' ', x + '</ref>')
    return x
    
def clean_wikitext(token_list):    
    x = ' '.join(token_list)

    # preemptively remove <ref>'s (including uncompleted)
    x = x.strip()
    x = rm_refs(x)
    # collapse multispaces
    x = re.sub('[ ]+', ' ', x)

    parse = mwparserfromhell.parse(x)
    plaintext = parse.strip_code()
    plaintext = rm_refs(plaintext) # get refs again? some things missed
    # collapse multispaces
    plaintext = re.sub('[ ]+', ' ', plaintext)
    # parse again to hit complicatd nested wikicode like 21055249
    parse = mwparserfromhell.parse(plaintext)
    plaintext = parse.strip_code()

    # ignore lines starting with ! or | (likely table artifacts)
    if plaintext.startswith('?') or plaintext.startswith('|'):
        plaintext = ''

    # ignore lines without text, e.g. ( , , , , ) or ]]
    if not re.findall('\w', plaintext):
        plaintext = ''

    # parse AGAIN again to hit remaining links e.g. 377258469
    plaintext = plaintext.replace('[ ', '[').replace(' ]', ']')
    parse = mwparserfromhell.parse(plaintext)
    plaintext = parse.strip_code()
    # at this point just rm all brackets
    plaintext = plaintext.replace(']', '').replace('[', '')
    # rm html
    plaintext = re.sub('http\S+', '', plaintext)
    # rm parents with nothing in them, e.g. (; )
    plaintext = re.sub('\([^\w]*\)', '', plaintext)
    # rm remining <del>, <ins> (valid tags should already have been taken parsed)
    plaintext = re.sub('<\/?(del|ins)([-\w=" <>]+)?>', '', plaintext)
    # fuck stars
    plaintext = plaintext.replace('*', '')
    # rm table fragments
    plaintext = re.sub('(right[ ]?\||thumb[ ]?\||frame[ ]?\||\d+px[ ]?\|)', '', plaintext)
    # ignore timestamp sentences
    if 'retrieved on' in plaintext.lower():
        plaintext = ''

    # remove tabs and newlines (those is our deliminators beeyotch)
    plaintext.replace('\t', ' ')
    plaintext.replace('\n', ' ')
    plaintext.replace('\r', '')
    # collapse multispaces (again again)
    plaintext = re.sub('[ ]+', ' ', plaintext).strip()

    return plaintext


def find_matches(a_list, b_list, delta=5):
    def BLEU(hyp, ref):
        # get ngram stats
        stats = []
        stats.append(len(hyp))
        stats.append(len(ref))
        for n in range(1, 5):
            s_ngrams = Counter(
                [tuple(hyp[i:i + n]) for i in range(len(hyp) + 1 - n)]
            )
            r_ngrams = Counter(
                [tuple(ref[i:i + n]) for i in range(len(ref) + 1 - n)]
            )
            stats.append(max([sum((s_ngrams & r_ngrams).values()), 0]))
            stats.append(max([len(hyp) + 1 - n, 0]))

        # get bleu from stats
        if len(list(filter(lambda x: x == 0, stats))) > 0:
            return 0
        (c, r) = stats[:2]
        log_bleu_prec = sum(
            [math.log(float(x) / y) for x, y in zip(stats[2::2], stats[3::2])]
        ) / 4.
        bleu = math.exp(min([0, 1 - float(r) / c]) + log_bleu_prec)

        return 100 * bleu

    for i in range(len(a_list)):
        neighborhood_bleus = [
            (BLEU(a_list[i].split(), b_list[j].split()), j)
            for j in range(max(i - delta, 0), min(i + delta, len(b_list) - 1))
        ]
        # corner case: len(a_list) >> len(b_list)
        if not neighborhood_bleus:
            continue
        
        max_bleu, match_idx = max(neighborhood_bleus)
        
        yield i, match_idx, max_bleu


def tokenize(s):
    global TOKENIZER
    tok_list = TOKENIZER.tokenize(s.strip())
    return ' '.join(tok_list)



def sent_generator(revisions):
    global CTR_EMPTY_REV
    global CTR_MULTIPLE_EDITS
    global CTR_FAILED_CLEANING

    for rev_id in tqdm(revisions):
        prevs, posts = revisions[rev_id]

        # empty revision
        if not prevs or not posts:
            CTR_EMPTY_REV += 1
            continue
            
        # unicode dat shit
        if isinstance(prevs[0], bytes):
            prevs = [x.decode() for x in prevs]
        if isinstance(posts[0], bytes):
            posts = [x.decode() for x in posts]

        # multiple edits
        if len(prevs) > 1 or len(posts) > 1:
            CTR_MULTIPLE_EDITS += 1
            continue
            
        # print(prevs)
        prev_text = clean_wikitext(prevs).lower()
        post_text = clean_wikitext(posts).lower()
        print(prev_text)
        print(post_text)
        print()
        # failed cleaning
        if not prev_text or not post_text:
            CTR_FAILED_CLEANING += 1
            continue

        prev_sents_raw = sent_tokenize(prev_text)
        post_sents_raw = sent_tokenize(post_text)
        
        prev_sents_tok = [tokenize(s) for s in prev_sents_raw]
        post_sents_tok = [tokenize(s) for s in post_sents_raw]

        for i, j, score in find_matches(prev_sents_tok, post_sents_tok):
            yield prev_sents_raw[i], prev_sents_tok[i], post_sents_raw[j], post_sents_tok[j], score, rev_id

        # no sents


def is_spelling_diff(d):
    """takes a word diff as arg"""
    global SPELLCHECKER

        # only look at the one-word diffs
    if sum([len(chunk) for tag, chunk in d if tag == '-']) > 1:
        return False

    for i, (tag, words) in enumerate(d):
        if tag == '-' and i+1 < len(d) - 1 and len(words) == 1 and d[i+1][0] == '+':
            # is one-word spelling replacement
            correction = spell(words[0])
            if not correction == words[0] and correction in ' '.join(d[i+1][1]):
                return True

    return False


def get_tok_labels(s_diff):
    tok_labels = []
    for tag, chunk in s_diff:
        if tag == '=':
            tok_labels += ['0'] * len(chunk)
        elif tag == '-':
            tok_labels += ['1'] * len(chunk)
        else:
            pass

    return tok_labels


def should_keep(prev_raw, prev_tok, post_raw, post_tok, bleu, rev_id):
    global CTR_LOW_BLEU
    global CTR_LOW_LEVEN
    global CTR_TOO_MANY_1_TOKS
    global CTR_SPELLING

    # KEEP -- exact match
    if bleu == 100:
        return True, None, '0', ['0' for _ in range(len(prev_tok.split()))]
    # clearly not a match
    if bleu < 10.0:
        CTR_LOW_BLEU += 1
        return False, None, None, None
    # too close
    if Levenshtein.distance(prev_tok, post_tok) < 4:
        CTR_LOW_LEVEN += 1
        return False, None, None, None

    tok_diff = diff(prev_tok.split(), post_tok.split())
    tok_labels = get_tok_labels(tok_diff)
    assert len(tok_labels) == len(prev_tok.split())

    # too dissimilar -- less than half of toks shared
    tok_nums = [int(x) for x in tok_labels]
    if ( sum(tok_nums) * 1.0 / len(tok_nums) ) > 0.5:
        CTR_TOO_MANY_1_TOKS += 1
        return False, None, None, None  

    # edit was just fixing a spelling error
    word_diff = diff(word_tokenize(prev_raw), word_tokenize(post_raw))
    if is_spelling_diff(word_diff):
        CTR_SPELLING += 1
        return False, None, None, None

    single_word_edit = sum([len(chunk) for tag, chunk in word_diff if tag == '-']) == 1

    return True, single_word_edit, '1', tok_labels


# load big pickle 
# https://stackoverflow.com/questions/31468117/python-3-can-pickle-handle-byte-objects-larger-than-4gb
print('LOADING PICKLE...')

# revisions = pickle.load(open(pickle_path, 'rb'))
# bytes_in = bytearray(0)
# max_bytes = 2**31 - 1
# input_size = os.path.getsize(pickle_path)
# with open(pickle_path, 'rb') as f_in:
#     for _ in range(0, input_size, max_bytes):
#         bytes_in += f_in.read(max_bytes)
# revisions = pickle.loads(bytes_in)

revisions = {l.split('\t')[0]: [x.split('<EDIT-DELIM>') for x in l.split('\t')[1:]] for l in open(crawl_path) if len(l.split('\t')) == 3}

print('EXTRACTING EXAMPLES...')

out = []
for pre_post_bleu_id in sent_generator(revisions):
    keep, is_word_edit, sent_label, tok_labels = should_keep(*pre_post_bleu_id)
    # filtered out
    if not keep: continue

    prev_raw, prev_tok, post_raw, post_tok, _, rev_id = pre_post_bleu_id
    length_ratio = len(prev_raw) * 1.0 / len(post_raw)

    # # false edit
    # if is_word_edit is not None and sum([int(x) for x in tok_labels]) == 0:
    #     CTR_FALSE_POSITIVE += 1
    #     continue

    out.append({
        'is_word_edit': is_word_edit,
        'length_ratio': length_ratio,
        'rev_id': rev_id,
        'out_row': '\t'.join([
            rev_id, 
            # should already be done but w/e just to be safe
            prev_tok.strip().replace('\n', ' ').replace('\t', ' '), 
            post_tok.strip().replace('\n', ' ').replace('\t', ' '), 
            prev_raw.strip().replace('\n', ' ').replace('\t', ' '), 
            post_raw.strip().replace('\n', ' ').replace('\t', ' '), 
            sent_label, ' '.join(tok_labels)
        ])
    })

# ratio thresholding
ratios = [x['length_ratio'] for x in out if out['is_word_edit'] is not None]
N = len(ratios) * 1.0 
mu = np.mean(ratios)
sd = np.std(ratios)


print('WRITING...')
# write unbiased
f_unbiased = open(out_prefix + '.unbiased', 'w')
f_biased = open(out_prefix + '.biased', 'w')
f_word = open(out_prefix + '.wordbiased', 'w')
f_length_skipped = open(out_prefix + '.biased_ratioskipped', 'w')

for ex in out:
    if ex['is_word_edit'] is None:
        f_unbiased.write(ex['out_row'] + '\n')
        continue

    # ratio skip
    r = ex['length_ratio']
    if (r < mu - 2.0 * sd) or (r > mu + 2.0 * sd):
        f_length_skipped.write(ex['out_row'] + '\n')
        CTR_LENGTH_RATIO += 1
        continue

    if ex['is_word_edit']:
        f_word.write(ex['out_row'] + '\n')

    f_biased.write(ex['out_row'] + '\n')


            
f_unbiased.close()
f_biased.close()
f_word.close()
f_length_skipped.close()

print('ctrs:')

print('CTR_EMPTY_REV', CTR_EMPTY_REV)
print('CTR_MULTIPLE_EDITS', CTR_MULTIPLE_EDITS)
print('CTR_FAILED_CLEANING', CTR_FAILED_CLEANING)
print('CTR_LOW_BLEU', CTR_LOW_BLEU)
print('CTR_LOW_LEVEN', CTR_LOW_LEVEN)
print('CTR_TOO_MANY_1_TOKS', CTR_TOO_MANY_1_TOKS)
print('CTR_SPELLING', CTR_SPELLING)
print('CTR_FALSE_POSITIVE', CTR_FALSE_POSITIVE)
print('CTR_LENGTH_RATIO', CTR_LENGTH_RATIO)



