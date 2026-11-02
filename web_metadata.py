# ==========================================
# StoryPulse Private v3.0
# Created by FacuSecX
# https://github.com/FacuSecX/StoryPulse-Private
# ==========================================

"""Isolated web reader. Discovers the current read descriptor; never opens a viewer.

No production deployment. Authentication, returned bodies and media URLs stay in
memory. All browser writes are denied except the exact discovered read operation.
"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

from job_fallback import (AuthenticationFailure, PermissionFailure, ProtocolFailure,
                          RateLimitFailure, TransientFailure)


def descriptors_from_scripts(scripts):
    """Recognize a read capability, not a fixed operation name or document ID."""
    modules = []
    artifacts = []
    for body in scripts:
        starts = list(re.finditer(r'''__d\s*\(\s*["']([^"'\n]+)["']''', body))
        for index, match in enumerate(starts):
            end = starts[index + 1].start() if index + 1 < len(starts) else len(body)
            chunk = body[match.start():end]
            modules.append((match[1], chunk))
            if (match[1].endswith('$Parameters')
                    and re.search(r'''operationKind\s*:\s*["']query["']''', chunk)
                    and re.search(r'''["'][^"']*reels_media["']''',chunk)
                    and 'root_field_name' in chunk):
                artifacts.append(match[1])
    found = []
    for artifact in dict.fromkeys(artifacts):
        for entry, chunk in modules:
            if (re.search(r'getPreloadProps\s*:\s*function', chunk)
                    and re.search(r'''["']'''+re.escape(artifact)+r'''["']''',chunk)):
                found.append((artifact, entry))
    return found


def json_documents(text, content_type=None):
    if not isinstance(text, str) or len(text) > 12_000_000:
        raise ProtocolFailure('Web metadata body is missing or too large')
    if 'multipart/' in str(content_type).lower():
        match=re.search(r'boundary\s*=\s*(?:"([^"\r\n]+)"|([^;\s\r\n]+))',str(content_type),re.I)
        boundary=(match[1] or match[2]) if match else ''
        if not boundary or len(boundary)>70:
            raise ProtocolFailure('The multipart metadata boundary is invalid')
        pieces=text.split('--'+boundary)
        if pieces[0].strip() or len(pieces)<3 or pieces[-1].strip()!='--' or len(pieces)>102:
            raise ProtocolFailure('The multipart metadata response is incomplete')
        result=[]
        for part in pieces[1:-1]:
            part=part.lstrip('\r\n')
            headers,sep,payload=part.partition('\r\n\r\n')
            if not sep:headers,sep,payload=part.partition('\n\n')
            if not sep or len(headers)>8192:
                raise ProtocolFailure('The multipart metadata headers are invalid')
            if not re.search(r'^content-type\s*:\s*(?:application/(?:[\w.+-]*\+)?json|text/javascript)(?:\s*;|\s*$)',headers,re.I|re.M):
                raise ProtocolFailure('The multipart metadata part is not JSON')
            result.extend(json_documents(payload))
            if len(result)>100:raise ProtocolFailure('Too many metadata documents')
        return result
    text = text.lstrip('\ufeff \t\r\n')
    text=re.sub(r'^(?:for\s*\(\s*;\s*;\s*\)|while\s*\(\s*1\s*\))\s*;\s*','',text,count=1)
    if text.startswith(")]}'"):text=text[4:].lstrip(', \t\r\n')
    decoder = json.JSONDecoder()
    documents = []
    try:
        while text.strip():
            text = text.lstrip()
            doc, end = decoder.raw_decode(text)
            if not isinstance(doc, dict):
                raise ValueError()
            documents.append(doc)
            if len(documents) > 100:
                raise ValueError()
            text = text[end:]
    except (ValueError, TypeError, RecursionError):
        raise ProtocolFailure('The web reader did not return recognized JSON metadata') from None
    if not documents:
        raise ProtocolFailure('The web reader returned an empty response')
    return documents


def _bounded_document(value):
    stack=[(value,0)];count=0
    while stack:
        item,depth=stack.pop();count+=1
        if depth>64 or count>150000:
            raise ProtocolFailure('The metadata structure exceeds its limits')
        if isinstance(item,dict):stack.extend((v,depth+1) for v in item.values())
        elif isinstance(item,list):stack.extend((v,depth+1) for v in item)


def _merge_values(old,new):
    if old is None:return deepcopy(new)
    if isinstance(old,dict) and isinstance(new,dict):
        for key,value in new.items():
            old[key]=_merge_values(old[key],value) if key in old else deepcopy(value)
        return old
    if isinstance(old,list) and isinstance(new,list):
        for index,value in enumerate(new):
            if index<len(old):old[index]=_merge_values(old[index],value)
            else:old.append(deepcopy(value))
        return old
    if type(old) is type(new) and old==new:return old
    raise ProtocolFailure('The metadata stream contains conflicting values')


def assemble_graphql_documents(documents,roots,validate):
    """Assemble recognized GraphQL defer/stream patches without accepting gaps."""
    if not isinstance(roots,list) or not roots or not all(isinstance(r,str) for r in roots):
        raise ProtocolFailure('The query has no recognized roots')
    merged={};pending=False;saw_data=False;patches=0
    def continuation(obj):
        nonlocal pending
        flags=[obj[k] for k in ('has_next','hasNext') if k in obj]
        extension=obj.get('extensions')
        if isinstance(extension,dict) and 'is_final' in extension:
            if type(extension['is_final']) is not bool:
                raise ProtocolFailure('The metadata final flag is invalid')
            flags.append(not extension['is_final'])
        if flags:
            if any(type(f) is not bool for f in flags) or len(set(flags))>1:
                raise ProtocolFailure('The metadata continuation flag is invalid')
            pending=flags[0]
    def apply_patch(patch):
        nonlocal patches
        patches+=1
        if patches>500:raise ProtocolFailure('Too many metadata patches')
        validate(patch)
        if patch.get('errors'):raise ProtocolFailure('Instagram rejected part of the metadata')
        path=patch.get('path')
        if (not isinstance(path,list) or not 1<=len(path)<=64 or path[0] not in roots
                or any(type(p) not in (str,int) or isinstance(p,int) and p<0 for p in path)):
            raise ProtocolFailure('The metadata patch path is invalid')
        parent=merged
        for segment in path[:-1]:
            if isinstance(parent,dict) and isinstance(segment,str) and segment in parent:parent=parent[segment]
            elif isinstance(parent,list) and type(segment) is int and segment<len(parent):parent=parent[segment]
            else:raise ProtocolFailure('The metadata stream contains a gap')
        last=path[-1]
        if ('data' in patch)==('items' in patch):raise ProtocolFailure('The metadata patch is ambiguous')
        if 'data' in patch:
            if isinstance(parent,dict) and isinstance(last,str):
                parent[last]=_merge_values(parent[last],patch['data']) if last in parent else deepcopy(patch['data'])
            elif isinstance(parent,list) and type(last) is int and last<=len(parent):
                if last==len(parent):parent.append(deepcopy(patch['data']))
                else:parent[last]=_merge_values(parent[last],patch['data'])
            else:raise ProtocolFailure('The metadata patch target is invalid')
        else:
            values=patch['items']
            if not isinstance(values,list):raise ProtocolFailure('The stream patch is not a list')
            if isinstance(parent,list) and type(last) is int and last<=len(parent):target,start=parent,last
            elif isinstance(parent,dict) and isinstance(last,str) and isinstance(parent.get(last),list):target,start=parent[last],len(parent[last])
            else:raise ProtocolFailure('The stream patch target is invalid')
            for offset,value in enumerate(values):
                index=start+offset
                if index<len(target):target[index]=_merge_values(target[index],value)
                else:target.append(deepcopy(value))
        continuation(patch)
    for doc in documents:
        _bounded_document(doc);validate(doc)
        _require_complete_page(doc)
        if doc.get('errors'):raise ProtocolFailure('Instagram did not complete the metadata query')
        continuation(doc)
        if 'path' in doc:
            apply_patch(doc)
        elif 'data' in doc:
            data=doc['data']
            if not isinstance(data,dict):raise ProtocolFailure('The metadata document has no data object')
            _require_complete_page(data)
            merged=_merge_values(merged,{k:v for k,v in data.items() if k in roots})
            saw_data=True
        incrementals=doc.get('incremental',[])
        if not isinstance(incrementals,list):raise ProtocolFailure('The metadata incremental list is invalid')
        for patch in incrementals:
            if not isinstance(patch,dict):raise ProtocolFailure('The metadata patch is invalid')
            apply_patch(patch)
        if not any(k in doc for k in ('data','path','incremental','extensions','has_next','hasNext')):
            raise ProtocolFailure('The metadata document has an unknown envelope')
        _bounded_document(merged)
    if pending:raise ProtocolFailure('The metadata stream ended before its final response')
    if not saw_data:raise ProtocolFailure('The metadata response has no initial data')
    return merged


def _require_complete_page(container):
    """Inspect pagination only on the envelope/container/reel being accepted."""
    if not isinstance(container,dict):return
    for key in ('has_more','more_available','has_next_page','has_previous_page'):
        if key in container and container[key] is not False and container[key] is not None:
            raise ProtocolFailure('The web metadata indicates incomplete or unrecognized pagination')
    page=container.get('page_info')
    if isinstance(page,dict):
        for key in ('has_more','more_available','has_next_page','has_previous_page'):
            if key in page and page[key] is not False and page[key] is not None:
                raise ProtocolFailure('The web reel connection requires unsupported pagination')


def reels_from_data(data,roots):
    """Accept explicit complete containers, never infer emptiness from unknown data."""
    _require_complete_page(data)
    reels=[];recognized=False
    for key in roots:
        if 'reels_media' not in key or key not in data:continue
        value=data[key]
        _require_complete_page(value)
        if isinstance(value,list):current=value
        elif isinstance(value,dict):
            page=value.get('page_info')
            if isinstance(page,dict) and (page.get('has_next_page') is True or page.get('has_previous_page') is True):
                raise ProtocolFailure('The web reel connection requires unsupported pagination')
            if isinstance(value.get('reels_media'),list):current=value['reels_media']
            elif isinstance(value.get('reels'),dict):current=list(value['reels'].values())
            elif isinstance(value.get('edges'),list) and isinstance(page,dict) and page.get('has_next_page') is False:
                if any(not isinstance(e,dict) or not isinstance(e.get('node'),dict) for e in value['edges']):
                    raise ProtocolFailure('The web reel connection is malformed')
                current=[e['node'] for e in value['edges']]
            elif isinstance(value.get('items'),list) and isinstance(value.get('user'),dict):current=[value]
            else:raise ProtocolFailure('The web Stories container has an unknown structure')
        else:raise ProtocolFailure('The web Stories container is not recognized')
        recognized=True;reels.extend(current)
    if not recognized:raise ProtocolFailure('The expected web Stories data was not returned')
    found={}
    for reel in reels:
        _require_complete_page(reel)
        if not isinstance(reel,dict) or not isinstance(reel.get('id'),(str,int)):
            raise ProtocolFailure('The web reel has no identity')
        key=str(reel['id'])
        if key in found and found[key]!=reel:raise ProtocolFailure('The metadata repeats an inconsistent reel')
        found[key]=reel
    return list(found.values())


_DESCRIPTOR_JS = r'''async ({artifact,entry,uid})=>{
 const load=name=>new Promise((resolve,reject)=>{let t=setTimeout(()=>reject(new Error("module_timeout")),5000);requireLazy([name],m=>{clearTimeout(t);resolve(m&&m.default||m)})});
 const p=await load(artifact),e=await load(entry);
 if(p.params.operationKind!=="query")throw new Error("not_read_query");
 const data=e.getPreloadProps({routeProps:{user_id:uid},routeParams:{},passthroughProps:{}});
 const q=Object.values(data.queries).find(q=>q.parameters.params.id===p.params.id);
 if(!q)throw new Error("missing_query");
 const variables={...q.variables};
 for(const [key,provider] of Object.entries(p.params.providedVariables||{}))variables[key]=provider.get();
 return {id:p.params.id,name:p.params.name,variables,roots:p.params.metadata.root_field_name,kind:p.params.operationKind};
}'''

_FETCH_JS = r'''async d=>{
 const load=name=>new Promise((resolve,reject)=>{let t=setTimeout(()=>reject(new Error("module_timeout")),5000);requireLazy([name],m=>{clearTimeout(t);resolve(m&&m.default||m)})});
 const [lsd,dtsg,user]=await Promise.all([load("LSD"),load("DTSGInitialData"),load("CurrentUserInitialData")]);
 const body=new URLSearchParams({av:String(user.IG_USER_EIMU||user.USER_ID||user.id||"0"),__a:"1",__d:"www",fb_api_caller_class:"RelayModern",fb_api_req_friendly_name:d.name,variables:JSON.stringify(d.variables),server_timestamps:"true",doc_id:d.id,lsd:lsd.token,fb_dtsg:dtsg.token});
 const csrf=document.cookie.split("; ").find(v=>v.startsWith("csrftoken="));
 const headers={"Content-Type":"application/x-www-form-urlencoded","X-FB-LSD":lsd.token};
 if(csrf)headers["X-CSRFToken"]=decodeURIComponent(csrf.split("=").slice(1).join("="));
 const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),30000);
 try{
   const response=await fetch("/api/graphql",{method:"POST",credentials:"same-origin",redirect:"error",headers,body,signal:controller.signal});
   return {status:response.status,type:response.headers.get("content-type"),text:await response.text()};
 } finally {clearTimeout(timer)}
}'''


class WebMetadataReader:
    def __init__(self, playwright, context_factory, normalize_reels):
        self.playwright = playwright
        self.context_factory = context_factory
        self.normalize_reels = normalize_reels
        self.browser = self.context = self.page = None
        self._scripts = {}
        self._approved = {}
        self._descriptors = []
        self.audit = {'read_calls': 0, 'blocked_writes': 0, 'blocked_seen': 0,
                      'viewer_opened': False, 'session_saved': False,
                      'descriptors_attempted': 0, 'descriptor_protocol_failures': 0}
        self.last_seen = None
        self.last_item_count = 0
        self.initial_html = ''

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        try:
            if self.context is not None:
                self.context.close()
        finally:
            if self.browser is not None:
                self.browser.close()
            self.page = self.context = self.browser = None

    def _guard(self, route):
        req = route.request
        url = urlsplit(req.url)
        path = url.path.lower()
        if any(token in path for token in ('/seen', '/mark_seen', '/mark_viewed', '/stories/')):
            self.audit['blocked_seen'] += 1
            route.abort()
            return
        if req.method not in ('GET', 'HEAD'):
            try:
                form = parse_qs(req.post_data or '', strict_parsing=False)
                doc_id = form.get('doc_id', [''])[0]
                approved = self._approved.get(doc_id)
                variables = json.loads(form.get('variables', ['{}'])[0])
                if (req.method == 'POST' and url.scheme == 'https'
                        and url.hostname == 'www.instagram.com'
                        and not url.query and not url.username and not url.password
                        and path.rstrip('/') == '/api/graphql' and approved is not None
                        and form.get('fb_api_req_friendly_name') == [approved['name']]
                        and variables == approved['variables']):
                    self.audit['read_calls'] += 1
                    route.continue_()
                    return
            except (ValueError, TypeError):
                pass
            self.audit['blocked_writes'] += 1
            route.abort()
            return
        if req.resource_type in ('image', 'media', 'font'):
            route.abort()
            return
        trusted = (url.hostname in ('www.instagram.com', 'instagram.com')
                   or str(url.hostname).endswith(('.cdninstagram.com', '.fbcdn.net')))
        if url.scheme != 'https' or not trusted:
            route.abort()
            return
        if '/graphql' in path:
            route.abort()
            return
        route.continue_()

    def _received(self, response):
        if (response.request.resource_type != 'script' or response.status != 200
                or len(self._scripts) >= 85):
            return
        try:
            body = response.text()
            if len(body) < 7_000_000 and sum(map(len, self._scripts.values())) < 35_000_000:
                self._scripts[response.url] = body
        except Exception:
            pass

    def _prepare(self, username):
        if self.page is not None:
            if not self._descriptors:
                raise ProtocolFailure('No compatible read-only web operation is available')
            return
        if not re.fullmatch(r'[A-Za-z0-9._]{1,30}', username):
            raise ValueError('Invalid profile')
        try:
            self.browser, self.context = self.context_factory(self.playwright)
            self.context.route('**/*', self._guard)
            self.page = self.context.new_page()
            self.page.on('response', self._received)
            response = self.page.goto('https://www.instagram.com/' + username + '/',
                                      wait_until='domcontentloaded', timeout=45000)
            if response is None:
                raise TransientFailure('The web profile did not respond')
            path = urlsplit(self.page.url).path.lower()
            if path.startswith('/accounts/login'):
                raise AuthenticationFailure('Instagram requires login')
            if any(x in path for x in ('/challenge', '/checkpoint')):
                raise AuthenticationFailure('Instagram requires verification')
            self._check_status(response.status)
            self.page.wait_for_timeout(4000)
            self.initial_html = self.page.content()
            self._descriptors = descriptors_from_scripts(self._scripts.values())
            if not self._descriptors:
                raise ProtocolFailure('No recognized read-only web Stories operation was discovered')
            self._scripts.clear()
        except (ProtocolFailure, TransientFailure, AuthenticationFailure,
                PermissionFailure, RateLimitFailure, ValueError):
            raise
        except Exception:
            raise TransientFailure('The isolated web context could not be prepared') from None

    @staticmethod
    def _check_status(status):
        if status == 429:
            raise RateLimitFailure('Instagram requested a pause')
        if status == 401:
            raise AuthenticationFailure('Instagram requires login')
        if status == 403:
            raise PermissionFailure('Instagram rejected the web read')
        if status >= 500:
            raise TransientFailure('Instagram returned a temporary server error')
        if status != 200:
            raise ProtocolFailure('Instagram returned an unsupported web response')

    def _query(self, artifact, entry, requested):
        try:
            descriptor = self.page.evaluate(_DESCRIPTOR_JS,
                                            {'artifact': artifact, 'entry': entry, 'uid': requested})
        except Exception:
            raise ProtocolFailure('The discovered web operation could not prepare its variables') from None
        if not isinstance(descriptor,dict):
            raise ProtocolFailure('The discovered read descriptor is invalid')
        _bounded_document(descriptor)
        def contains_target(value):
            if isinstance(value,dict):return any(contains_target(x) for x in value.values())
            if isinstance(value,list):return any(contains_target(x) for x in value)
            return not isinstance(value,bool) and isinstance(value,(str,int)) and str(value)==requested
        if (descriptor.get('kind') != 'query'
                or not re.fullmatch(r'[0-9]{8,30}', str(descriptor.get('id', '')))
                or not isinstance(descriptor.get('name'),str)
                or not re.fullmatch(r'[A-Za-z0-9_$]{1,200}',descriptor['name'])
                or not isinstance(descriptor.get('variables'),dict)
                or not isinstance(descriptor.get('roots'),list)
                or not 1<=len(descriptor['roots'])<=32
                or not all(isinstance(root,str) for root in descriptor['roots'])
                or not any(root.endswith('reels_media') for root in descriptor['roots'])
                or not contains_target(descriptor.get('variables', {}))
                or len(json.dumps(descriptor.get('variables',{})))>65536):
            raise ProtocolFailure('The discovered web operation did not target the requested reel')
        self._approved = {descriptor['id']: descriptor}
        try:
            try:
                result = self.page.evaluate(_FETCH_JS, descriptor)
            except Exception:
                raise TransientFailure('The isolated web metadata request could not complete') from None
        finally:
            self._approved.clear()
        from instagram_sessions import validar_datos_instagram
        status=int(result['status'])
        try:
            documents = json_documents(result['text'],result.get('type'))
        except ProtocolFailure:
            self._check_status(status)
            raise
        for document in documents:
            validar_datos_instagram(document)
        self._check_status(status)
        data=assemble_graphql_documents(documents,descriptor['roots'],validar_datos_instagram)
        return reels_from_data(data,descriptor['roots'])

    def consultar(self, username, owner_id, reel_id=None):
        owner = str(owner_id) if owner_id is not None else None
        requested = str(reel_id or owner)
        if not re.fullmatch(r'[A-Za-z0-9._]{1,30}',username):
            raise ValueError('Invalid profile')
        if owner is not None and not re.fullmatch(r'[1-9][0-9]{0,39}', owner):
            raise ValueError('Invalid owner')
        if owner is None and not requested.startswith('highlight:'):
            raise ValueError('Stories require a known owner')
        if requested != owner and not re.fullmatch(r'highlight:[1-9][0-9]{0,39}', requested):
            raise ValueError('Invalid reel')
        self._prepare(username)
        for artifact,entry in list(dict.fromkeys(self._descriptors))[:4]:
            self.audit['descriptors_attempted']+=1
            try:
                reels=self._query(artifact,entry,requested)
                items=self._validated_items(reels,username,owner,requested)
                return items
            except ProtocolFailure:
                self.audit['descriptor_protocol_failures']+=1
        raise ProtocolFailure('The discovered web read operations did not return valid Stories metadata')

    def _validated_items(self,reels,username,owner,requested):
        if not reels:
            self.last_item_count = 0
            self.last_seen=[]
            return []
        if any(not isinstance(reel, dict) or str(reel.get('id')) != requested for reel in reels):
            raise ProtocolFailure('The web reply included a different reel')
        if requested.startswith('highlight:'):
            for reel in reels:
                user=reel.get('user')
                if not isinstance(user,dict) or str(user.get('username','')).casefold()!=username.casefold():
                    raise ProtocolFailure('The highlighted reel belongs to another profile')
                actual=str(user.get('pk',user.get('id','')))
                if not re.fullmatch(r'[1-9][0-9]{0,39}',actual) or owner is not None and owner!=actual:
                    raise ProtocolFailure('The highlighted reel has an inconsistent owner')
                owner=actual
        try:
            items = self.normalize_reels(reels, owner, reel_id=requested if requested.startswith('highlight:') else None)
        except Exception as error:
            from instagram_sessions import ErrorConsultaInstagram
            if isinstance(error, ErrorConsultaInstagram):
                raise ProtocolFailure('The web reply failed owner or item validation') from None
            raise
        self.last_seen = [reel.get('seen') for reel in reels]
        self.last_item_count = len(items)
        return items
