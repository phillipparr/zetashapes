#!/usr/bin/python

from flask import Flask
import flask_gzip
import json
import re
from functools import wraps
from collections import namedtuple
from flask import redirect, request, current_app, jsonify
import psycopg2
import psycopg2.extras
from collections import defaultdict
import os
from itertools import groupby
from shapely.ops import cascaded_union
from shapely.geometry import mapping, asShape

VOTES_TABLE = 'votes_dev'
USER_VOTES_TABLE = 'user_votes_dev'

def pickBestVotesHelper(votes, preferSmear=True, preferOfficial=True):
  maxVote = None

  selfVotes = [v for v in votes if v['source'] == 'self']
  positiveSelfVotes = None
  negativeSelfVotes = []
  if len(selfVotes) > 0:
    negativeSelfVotes = [v for v in selfVotes if v['count'] < 0]
    positiveSelfVotes = [v for v in selfVotes if v['count'] > 0]
    if negativeSelfVotes and not positiveSelfVotes:
      pass
    else:
      votes = positiveSelfVotes
  if not maxVote and len(votes) > 0:
    maxVote = max(votes, key=lambda x:x['count'])

  negativeSelfBlocks = set([b['id'] for b in negativeSelfVotes])
  usersVotes = [v for v in votes if v['source'] == 'users' and v['id'] not in negativeSelfBlocks]
  if usersVotes and not positiveSelfVotes:
    usersVotes.sort(key=lambda x: x['count'] * -1)
    return [usersVotes[0],]
  
  officialVotes = [v for v in votes if v['source'].startswith('official')]
  if preferOfficial and officialVotes and not positiveSelfVotes:
    return [officialVotes[0],]

  blockrVotes = [v for v in votes if v['source'] == 'blockr']
  if preferSmear and blockrVotes and not positiveSelfVotes:
    return [blockrVotes[0],]

  smearVotes = [v for v in votes if v['source'] == 'smear']
  if preferSmear and smearVotes and not positiveSelfVotes:
    return [smearVotes[0],]
  
  if maxVote:
    return [maxVote,]

  return []
  
def pickBestVotes(votes, preferSmear=True, preferOfficial=True):
  maxVotes = pickBestVotesHelper(votes, preferSmear, preferOfficial)
  if maxVotes and maxVotes[0]['id'] == -1:
    return []
  else:
    return maxVotes

def getAreaIdsForUserId(conn, userId):
  cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
  cur.execute("""select blockid FROM  """ + USER_VOTES_TABLE + """  v JOIN geoplanet_places g ON v.woe_id = g.woe_id WHERE v.userid = %s""" % (userId))
  areaids = tuple(set([x['blockid'][0:5] for x in cur.fetchall()]))
  return areaids

def addUserVotes(userVoteRows, votesDict):
  dedupedRows = {}

  # take your first positive vote, unless you have a negative vote that comes after it that invalidates that
  for r in userVoteRows:
    if r['blockid'] in dedupedRows:
      if (
        dedupedRows[r['blockid']]['weight'] == 1 and
        dedupedRows[r['blockid']]['woe_id'] == r['woe_id'] and
        r['weight'] == -1
      ): 
        dedupedRows[r['blockid']] = r
      if r['weight'] == 1:
        dedupedRows[r['blockid']] = r
    else:
      dedupedRows[r['blockid']] = r

  for r in dedupedRows.values():
    votesDict[r['blockid']].append({
      'label': r['name'], 
      'id': r['woe_id'], 
      'source': 'self',
      'count': r['weight']
    })

def buildVoteDict(rows):
  votes = defaultdict(list)
  for r in rows:
    votes[r['id']].append({
      'label': r['name'], 
      'id': r['woe_id'], 
      'count': r['count'], 
      'source': r['source']
    })
  return votes

def getUserVotesForBlocks(conn, userId, blockids):
  cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
  comm = cur.mogrify("""select * FROM """ + USER_VOTES_TABLE + """ WHERE userid=%s AND blockid IN %s""", (
    userId,
    blockids
  ))
  #print comm
  cur.execute(comm)

  return cur.fetchall()

def getVotesForBlocks(conn, blockids, user):
  cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
  cur.execute("""select woe_id, id, label, count, v.source, name FROM """ + VOTES_TABLE + """ v JOIN geoplanet_places ON label::int = woe_id WHERE id IN %s""", (tuple(blockids),))
  votes = buildVoteDict(cur.fetchall())
  if user:
    print user
    userId = user['id']
    cur.execute("""select g.woe_id, blockid, name, weight FROM """ + USER_VOTES_TABLE + """ v JOIN geoplanet_places g ON v.woe_id = g.woe_id WHERE v.userid = %s AND v.blockid IN %s ORDER BY ts ASC""", (userId, tuple(blockids)))
    addUserVotes(cur.fetchall(), votes)
  return votes

def getVotes(conn, areaids, user): 
  cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

  areaid_clauses = []
  for areaid in areaids:
    statefp10 = areaid[0:2]
    countyfp10 = areaid[2:]
    areaid_clauses.append(cur.mogrify("statefp10 = %s AND countyfp10 = %s", (statefp10, countyfp10)))

  if not areaids:
    raise Exception("uh, missing areaid???")
  print areaid

  print 'getting blocks with geoms'
  cur.execute("""select geoid10, pop10, housing10, ST_AsGeoJSON(ST_Transform(geom, 4326)) as geojson_geom FROM tabblock2010_pophu tb WHERE (""" + ' OR '.join(areaid_clauses) + """) AND blockce10 NOT LIKE '0%%'""", (statefp10, countyfp10))
  rows = cur.fetchall()
  print 'got'


  print 'getting votes'
  id_clauses = []
  for areaid in areaids:
    id_clauses.append("id LIKE '%s%%'" % areaid)
  cur.execute("""select woe_id, id, label, count, v.source, name FROM """ + VOTES_TABLE + """ v JOIN geoplanet_places ON label::int = woe_id WHERE """ + ' OR '.join(id_clauses))
  globalVotes = cur.fetchall()
  print 'got'

  votes = buildVoteDict(globalVotes)

  id_clauses = []
  for areaid in areaids:
    id_clauses.append("v.blockid LIKE '%s%%'" % areaid)

  user_votes = {}
  print 'user? %s' % user
  print user
  if user:
    userId = user['id']
    print 'getting user votes'
    cur.execute("""select g.woe_id, blockid, name, weight FROM """ + USER_VOTES_TABLE + """ v JOIN geoplanet_places g ON v.woe_id = g.woe_id WHERE v.userid = %s """ % userId + """ AND (""" + ' OR '.join(id_clauses) + """) ORDER BY ts ASC""")
    print 'got'
    addUserVotes(cur.fetchall(), votes)
    
  return (rows, votes)

