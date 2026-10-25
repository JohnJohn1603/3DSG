#!/usr/bin/env python3
"""Versioned Kaggle entrypoint around pinned ConceptGraphs reference scripts.

This local repository does not install dependencies, fetch weights, upload data,
or launch remote compute. Run subcommands only from the Kaggle notebook after
the user has prepared the reference checkouts and checkpoints.
"""
from __future__ import annotations
import argparse, gzip, hashlib, json, os, shutil, subprocess, sys, threading, time, traceback
from pathlib import Path
from importlib import metadata as importlib_metadata
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from integration_v2.capture_package import sha256_file, validate_package
from integration_v2.object_export_contract import SCHEMA as EXPORT_SCHEMA, validate_object_export

REF_COMMIT = "93277a02bd89171f8121e84203121cf7af9ebb5d"
GSA_COMMIT = "a4d76a2b55e348943cba4cd57d7553c354296223"
WEIGHTS = ("ram_swin_large_14m.pth", "groundingdino_swint_ogc.pth", "sam_vit_h_4b8939.pth")
MODEL_RECIPE = {"tagger":"RAM/ramswin_large_14m", "detector":"GroundingDINO SwinT-OGC",
                "segmenter":"SAM ViT-H", "clip":"OpenCLIP ViT-H-14/laion2b_s32b_b79k"}

def _git_commit(path: Path) -> str:
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()

def _probe() -> dict:
    data={"python":sys.version,"platform":sys.platform,"gpus":[],"torch":None,"cuda":None}
    try:
        import torch
        data["torch"] = torch.__version__; data["cuda"] = torch.version.cuda
        data["cuda_available"] = torch.cuda.is_available()
        for i in range(torch.cuda.device_count()):
            props=torch.cuda.get_device_properties(i)
            free,total=torch.cuda.mem_get_info(i)
            data["gpus"].append({"index":i,"name":props.name,"total_vram_bytes":props.total_memory,
                                 "free_vram_bytes":free,"total_runtime_vram_bytes":total})
    except Exception as e: data["torch_error"]=repr(e)
    try:
        import shutil as _sh
        du=_sh.disk_usage("/kaggle/working")
        data["disk_working"]={"total":du.total,"free":du.free}
    except Exception: pass
    try:
        import resource
        data["host_ram_bytes"]={"total":int(open("/proc/meminfo").read().split("MemTotal:")[1].split()[0])*1024,
                                "available":int(open("/proc/meminfo").read().split("MemAvailable:")[1].split()[0])*1024}
    except Exception: pass
    return data

def _memory_sample(pid: int) -> tuple[int,list[int]]:
    rss=0
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"): rss=int(line.split()[1])*1024
    except Exception: pass
    vrams=[]
    try:
        out=subprocess.check_output(["nvidia-smi","--query-gpu=memory.used","--format=csv,noheader,nounits"],text=True,stderr=subprocess.DEVNULL)
        vrams=[int(x.strip())*1024*1024 for x in out.splitlines() if x.strip()]
    except Exception: pass
    return rss,vrams

def _run_stage(name: str, argv: list[str], cwd: Path, env: dict[str,str], log: Path) -> dict:
    started=time.time(); peak_rss=0; peak_vrams=[]
    with log.open("wb") as f:
        proc=subprocess.Popen(argv,cwd=cwd,env=env,stdout=f,stderr=subprocess.STDOUT)
        while proc.poll() is None:
            rss,vrams=_memory_sample(proc.pid); peak_rss=max(peak_rss,rss)
            if len(vrams)>len(peak_vrams): peak_vrams.extend([0]*(len(vrams)-len(peak_vrams)))
            for i,value in enumerate(vrams): peak_vrams[i]=max(peak_vrams[i],value)
            time.sleep(1)
        code=proc.returncode
    return {"name":name,"argv":argv,"cwd":str(cwd),"started_unix":started,
            "runtime_s":round(time.time()-started,3),"exit_code":code,
            "peak_process_rss_bytes_sampled":peak_rss,
            "peak_gpu_vram_bytes_sampled_by_device":[{"gpu_index":i,"bytes":value} for i,value in enumerate(peak_vrams)],
            "log":str(log)}

def _file_hashes(root: Path, skip: set[str] | None = None) -> dict[str,str]:
    skip=skip or set(); result={}
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.relative_to(root).as_posix() not in skip:
            result[p.relative_to(root).as_posix()]=sha256_file(p)
    return result

def _models(gsa: Path, home: Path) -> dict:
    paths={n:gsa/n for n in WEIGHTS}
    missing=[n for n,p in paths.items() if not p.is_file()]
    cache=home/".cache"
    clip_files=[]
    if cache.exists():
        # OpenCLIP 2.20.0's pinned ViT-H-14 registry entry is an HF Hub
        # checkpoint. Also allow its documented URL-cache filename, but only
        # at the OpenCLIP cache root. Do not accept another model's similarly
        # named file elsewhere in the cache.
        hf_root=cache/"huggingface"/"hub"/"models--laion--CLIP-ViT-H-14-laion2B-s32B-b79K"/"snapshots"
        if hf_root.exists(): clip_files.extend(hf_root.glob("*/open_clip_pytorch_model.bin"))
        url_cache=cache/"clip"/"open_clip_pytorch_model.bin"
        if url_cache.is_file(): clip_files.append(url_cache)
    clip_files=sorted({p.resolve() for p in clip_files if p.is_file()})
    return {"recipe":MODEL_RECIPE,"gsa_weights":{n:{"path":str(p),"sha256":sha256_file(p) if p.is_file() else None} for n,p in paths.items()},
            "openclip_cached_files":[{"path":str(p),"sha256":sha256_file(p)} for p in clip_files],
            "missing_gsa_weights":missing,"openclip_missing_at_probe":not bool(clip_files),
            "missing_openclip_checkpoint":not bool(clip_files)}

def _dependency_versions() -> dict[str,str|None]:
    result={}
    # Only packages on the selected perception + object-mapping call path.
    # chamferdist belongs to reference evaluation scripts and is not installed
    # or required by this pilot.
    for name in ("torch","torchvision","torchaudio","open-clip-torch","open3d","faiss-cpu","pytorch3d","gradslam","hydra-core","omegaconf","supervision","timm","opencv-python","natsort"):
        try: result[name]=importlib_metadata.version(name)
        except importlib_metadata.PackageNotFoundError: result[name]=None
    return result

def _prepare_headless_detector_script(reference: Path, output: Path) -> tuple[Path, str]:
    """Create the sole compatibility-patched reference file in this run's output.

    All other ConceptGraphs modules remain imported from the immutable pinned
    checkout. The only patch is the upstream GUI backend selector.
    """
    source_path=reference/"conceptgraph/scripts/generate_gsa_results.py"
    source=source_path.read_text()
    needle='matplotlib.use("TkAgg")'
    if source.count(needle)!=1:
        raise RuntimeError("expected one reference TkAgg selector; adaptation precondition failed")
    adapted=output/"compat/generate_gsa_results.py"
    adapted.parent.mkdir(parents=True,exist_ok=True)
    adapted.write_text(source.replace(needle,'matplotlib.use("Agg")'))
    return adapted,sha256_file(adapted)

def _export_final_map(work_scene: Path, output: Path, package_manifest: dict, run_manifest: dict) -> dict:
    native_dir=work_scene/"pcd_saves"
    post_candidates=list(native_dir.glob("full_pcd_ram_withbg_allclasses_*.pkl.gz"))
    post_candidates=[p for p in post_candidates if p.name.endswith("_post.pkl.gz")]
    if len(post_candidates)!=1: raise RuntimeError(f"expected one postprocessed native map, found {len(post_candidates)}")
    post=post_candidates[0]
    with gzip.open(post,"rb") as f: payload=__import__("pickle").load(f)
    objects=payload["objects"]
    exp=output/"export"; exp.mkdir(parents=True,exist_ok=True)
    frame_ids=[f["frame_id"] for f in package_manifest["frames"]]
    observations=[]; detections_dir=work_scene/"gsa_detections_ram_withbg_allclasses"
    for fi,frame in enumerate(package_manifest["frames"]):
        detfile=detections_dir/f"{fi:06d}.pkl.gz"
        if not detfile.is_file(): raise RuntimeError(f"missing per-frame ConceptGraphs detection output: {detfile}")
        with gzip.open(detfile,"rb") as f: gobs=__import__("pickle").load(f)
        masks=np.asarray(gobs.get("mask",[])); feats=np.asarray(gobs.get("image_feats",[])); texts=np.asarray(gobs.get("text_feats",[]))
        conf=np.asarray(gobs.get("confidence",[])); classes=list(gobs.get("classes",[])); clsids=np.asarray(gobs.get("class_id",[]))
        if masks.ndim != 3: raise RuntimeError(f"unexpected mask array shape in {detfile}: {masks.shape}")
        if len(feats)!=len(masks) or len(texts)!=len(masks):
            raise RuntimeError(f"per-mask image/text feature count mismatch in {detfile}")
        if len(conf)!=len(masks) or len(clsids)!=len(masks):
            raise RuntimeError(f"per-mask class/confidence count mismatch in {detfile}")
        for di,mask in enumerate(masks):
            oid=f"{frame['frame_id']}:det-{di:04d}"; rel=f"observations/{fi:06d}/det-{di:04d}.npz"
            p=exp/rel; p.parent.mkdir(parents=True,exist_ok=True)
            np.savez_compressed(p,mask=np.asarray(mask,dtype=bool),
                                image_feature=feats[di] if len(feats)>di else np.empty((0,),np.float32),
                                text_feature=texts[di] if len(texts)>di else np.empty((0,),np.float32))
            cidx=int(clsids[di]) if len(clsids)>di else -1
            observations.append({"observation_id":oid,"frame_index":fi,"mask_index":di,
                                 "class_name":classes[cidx] if 0<=cidx<len(classes) else None,
                                 "confidence":float(np.asarray(conf[di]).reshape(-1)[0]),
                                 "mask_feature_file":rel,"source_detection_sha256":sha256_file(detfile)})
    members_by_obj=[]; object_rows=[]
    for oi,obj in enumerate(objects):
        xyz=np.asarray(obj.get("pcd_np",[]),dtype=np.float32)
        if xyz.ndim!=2 or xyz.shape[1]!=3 or len(xyz)==0: continue
        frame_ix=list(obj.get("image_idx",[])); mask_ix=list(obj.get("mask_idx",[]))
        if len(frame_ix)!=len(mask_ix): raise RuntimeError(f"native object membership arrays differ in length at snapshot item {oi}")
        members=[]
        for fi,mi in zip(frame_ix,mask_ix):
            if 0<=int(fi)<len(frame_ids): members.append(f"{frame_ids[int(fi)]}:det-{int(mi):04d}")
        cloud_name=f"objects/{oi:04d}_cloud.npz"; cloud=exp/cloud_name; cloud.parent.mkdir(parents=True,exist_ok=True)
        clip=np.asarray(obj.get("clip_ft",[]),dtype=np.float32); text=np.asarray(obj.get("text_ft",[]),dtype=np.float32)
        np.savez_compressed(cloud,xyz=xyz,clip_feature=clip,text_feature=text)
        # Snapshot key is content-derived and explicitly not an online persistent ID.
        digest=hashlib.sha256(xyz.tobytes()+"\n".join(sorted(members)).encode()).hexdigest()[:16]
        labels=obj.get("class_name",[])
        if clip.size==0 or text.size==0: raise RuntimeError(f"native object lacks fused CLIP/text features at snapshot item {oi}")
        object_rows.append({"export_id":f"final-map-{digest}","source_ordinal_for_this_snapshot_only":oi,
                            "class_name":labels[0] if isinstance(labels,list) and labels else (str(labels) if labels else None),
                            "num_detections":int(obj.get("num_detections",len(members))),"cloud_npz":cloud_name,
                            "point_count":int(len(xyz)),"bounds_minmax":[xyz.min(axis=0).tolist(),xyz.max(axis=0).tolist()],
                            "clip_feature_dimension":int(clip.size),"text_feature_dimension":int(text.size),
                            "membership_status":"available" if members else "unavailable","observation_membership":members})
    files=_file_hashes(exp)
    manifest={"schema":EXPORT_SCHEMA,"schema_version":1,"identity_scope":"final_map_only_no_persistent_lifecycle_claim",
              "scene_id":package_manifest["scene_id"],"coordinate_frame":"conceptgraphs_world_from_capture_contract",
              "T_capture_world_from_cg_world":np.eye(4).tolist(),
              "pose_note":"Mapping uses the absolute decoded canonical T_W_Copt from package; export coordinates are capture canonical world.",
              "native_map_file":str(post.relative_to(output)),"native_map_sha256":sha256_file(post),
              "input_package_content_sha256":package_manifest["content_sha256"],"run_config_sha256":run_manifest["effective_config_sha256"],
              "objects":object_rows,"observations":observations,"files_sha256":files}
    (exp/"export_manifest.json").write_text(json.dumps(manifest,indent=2)+"\n")
    return manifest

def _run(args) -> int:
    package=args.package.resolve(); ref=args.reference.resolve(); gsa=args.gsa.resolve(); out=args.output.resolve()
    if out.exists(): raise FileExistsError(f"output exists; choose a new run directory: {out}")
    pkg=validate_package(package)
    if _git_commit(ref)!=REF_COMMIT: raise RuntimeError("ConceptGraphs checkout is not at locked commit")
    if _git_commit(gsa)!=GSA_COMMIT: raise RuntimeError("Grounded-SAM checkout is not at the preflight-pinned commit")
    model_state=_models(gsa,Path.home())
    if model_state["missing_gsa_weights"]: raise RuntimeError(f"missing required GSA checkpoints: {model_state['missing_gsa_weights']}")
    if model_state["missing_openclip_checkpoint"]:
        raise RuntimeError("missing exact OpenCLIP ViT-H-14/laion2b_s32b_b79k checkpoint in the pinned OpenCLIP/Hugging Face cache; stage or mount it explicitly before running (network fallback is blocked by preflight)")
    pre_probe=_probe()
    if not pre_probe.get("cuda_available") or not pre_probe.get("gpus"):
        raise RuntimeError("no CUDA GPU visible; this run requires a Kaggle GPU and does not fall back to CPU")
    # Reference loads the four model families on one device. Two T4s are not pooled.
    if pre_probe["gpus"][0].get("free_vram_bytes",0) < 13 * 1024**3:
        raise RuntimeError("GPU 0 has under 13 GiB free; reference models share one device, so stop rather than splitting/changing the recipe")
    start,end=(0,1) if args.mode=="smoke" else (0,len(pkg["frames"]))
    if args.mode=="full" and len(pkg["frames"])!=7: raise RuntimeError("locked full pilot expects exactly seven frames")
    effective={"mode":args.mode,"start":start,"end":end,"stride":1,"device":"cuda:0","detector":{"class_set":"ram","detector":"dino","box_threshold":0.2,"text_threshold":0.2,"nms_threshold":0.5,"sam_variant":"sam","add_bg_classes":True,"accumu_classes":True,"exp_suffix":"withbg_allclasses"},
      "mapping":{"spatial_sim_type":"overlap","mask_conf_threshold":0.25,"match_method":"sim_sum","sim_threshold":1.2,"dbscan_eps":0.1,"gsa_variant":"ram_withbg_allclasses","skip_bg":False,"max_bbox_area_ratio":0.5,"mask_area_threshold":25,"min_points_threshold":16,"downsample_voxel_size":0.025,"dbscan_remove_noise":True,"dbscan_min_points":10,"obj_min_points":0,"obj_min_detections":3,"denoise_interval":20,"filter_interval":-1,"merge_interval":-1,"save_pcd":True},
      "reference_commit":REF_COMMIT,"gsa_commit":GSA_COMMIT,"package_content_sha256":pkg["content_sha256"]}
    ref_code_files=[ref/"conceptgraph/dataset/datasets_common.py",ref/"conceptgraph/slam/cfslam_pipeline_batch.py",
                    ref/"conceptgraph/slam/utils.py",ref/"conceptgraph/scripts/generate_gsa_results.py"]
    code_hashes={str(p.relative_to(ref)):sha256_file(p) for p in ref_code_files}
    runner_hash=sha256_file(Path(__file__))
    out.mkdir(parents=True); work=out/"workspace"; work.mkdir()
    shutil.copytree(package/"dataset"/"pilot_00808",work/"pilot_00808")
    shutil.copy2(package/"hm3d_adapter_dataconfig.yaml",out/"hm3d_adapter_dataconfig.yaml")
    cfg_hash=hashlib.sha256(json.dumps(effective,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    detector_cfg_hash=hashlib.sha256(json.dumps({"detector":effective["detector"],"start":start,"end":end,"stride":1},sort_keys=True,separators=(",",":")).encode()).hexdigest()
    mapping_cfg_hash=hashlib.sha256(json.dumps({"mapping":effective["mapping"],"start":start,"end":end,"stride":1},sort_keys=True,separators=(",",":")).encode()).hexdigest()
    manifest={"schema":"conceptgraphs-kaggle-run/v1","status":"running","effective_config":effective,
              "effective_config_sha256":cfg_hash,"input_package_manifest_sha256":sha256_file(package/"input_package_manifest.json"),
              "environment_lock_sha256":sha256_file(ROOT/"locks/integration_v2/cg_kaggle_environment.lock.json"),
              "runtime_dependency_versions":_dependency_versions(),
              "model_files":model_state,"code_hashes":{"reference":code_hashes,"runner_sha256":runner_hash},
              "stages":[],"output_root":str(out),"probe_before":pre_probe}
    (out/"run_manifest.json").write_text(json.dumps(manifest,indent=2)+"\n")
    env=os.environ.copy(); env["GSA_PATH"]=str(gsa); env["MPLBACKEND"]="Agg"
    # The detector compatibility copy is one file only. Explicit PYTHONPATH
    # resolves the rest of ConceptGraphs from the locked read-only checkout.
    existing_pythonpath=env.get("PYTHONPATH")
    env["PYTHONPATH"]=str(ref)+(os.pathsep+existing_pythonpath if existing_pythonpath else "")
    detector_cmd=[sys.executable,str(ref/"conceptgraph/scripts/generate_gsa_results.py"),"--dataset_root",str(work),"--dataset_config",str(out/"hm3d_adapter_dataconfig.yaml"),
       "--scene_id","pilot_00808","--start",str(start),"--end",str(end),"--stride","1","--desired_height","480","--desired_width","640",
       "--class_set","ram","--detector","dino","--box_threshold","0.2","--text_threshold","0.2","--nms_threshold","0.5",
       "--sam_variant","sam","--add_bg_classes","--accumu_classes","--exp_suffix","withbg_allclasses","--device","cuda:0"]
    # Keep the pinned checkout read-only and avoid copying unrelated tooling.
    # This one-file compatibility patch changes only the headless plotting backend.
    det_script,det_script_hash=_prepare_headless_detector_script(ref,out)
    detector_cmd[1]=str(det_script)
    stages=[_run_stage("conceptgraphs_detect",detector_cmd,ref,env,out/"detect.log")]
    stages[-1].update({"status":"complete" if stages[-1]["exit_code"]==0 else "failed",
                       "input_package_sha256":pkg["content_sha256"],"config_sha256":detector_cfg_hash,
                       "code_sha256":det_script_hash})
    manifest["stages"].extend(stages); manifest["status"]="running"; (out/"run_manifest.json").write_text(json.dumps(manifest,indent=2)+"\n")
    if stages[-1]["exit_code"]!=0: manifest["status"]="failed"; (out/"run_manifest.json").write_text(json.dumps(manifest,indent=2)+"\n"); return stages[-1]["exit_code"]
    detected=work/"pilot_00808/gsa_detections_ram_withbg_allclasses"
    found=sorted(detected.glob("*.pkl.gz")) if detected.is_dir() else []
    if len(found)!=(end-start):
        manifest["status"]="failed"; manifest["detection_output_validation"]={"expected":end-start,"found":len(found)}
        (out/"run_manifest.json").write_text(json.dumps(manifest,indent=2)+"\n")
        raise RuntimeError(f"ConceptGraphs detector output validation failed: expected {end-start} frames, found {len(found)}")
    stages[0]["output_validation"]={"status":"pass","detection_frame_files":len(found)}
    stages[0]["output_hashes"]=_file_hashes(detected)
    manifest["detection_output_validation"]={"expected":end-start,"found":len(found),"status":"pass"}
    (out/"run_manifest.json").write_text(json.dumps(manifest,indent=2)+"\n")
    mapping_script=ref/"conceptgraph/slam/cfslam_pipeline_batch.py"
    mapping_cmd=[sys.executable,str(mapping_script),f"dataset_root={work}",f"dataset_config={out/'hm3d_adapter_dataconfig.yaml'}","scene_id=pilot_00808",
      f"start={start}",f"end={end}","stride=1","spatial_sim_type=overlap","mask_conf_threshold=0.25","match_method=sim_sum","sim_threshold=1.2","dbscan_eps=0.1",
      "gsa_variant=ram_withbg_allclasses","skip_bg=False","max_bbox_area_ratio=0.5","mask_area_threshold=25","min_points_threshold=16","downsample_voxel_size=0.025",
      "dbscan_remove_noise=True","dbscan_min_points=10","obj_min_points=0","obj_min_detections=3","denoise_interval=20","filter_interval=-1","merge_interval=-1","save_pcd=True","class_agnostic=False","vis_render=False","debug_render=False","save_suffix=q1_cg_detect_v1"]
    stages.append(_run_stage("conceptgraphs_mapping",mapping_cmd,ref,env,out/"mapping.log"))
    stages[-1].update({"status":"complete" if stages[-1]["exit_code"]==0 else "failed",
                       "input_detections_sha256":hashlib.sha256(json.dumps(_file_hashes(detected),sort_keys=True).encode()).hexdigest(),
                       "config_sha256":mapping_cfg_hash,"code_sha256":sha256_file(mapping_script)})
    manifest["stages"]=stages; manifest["status"]="completed" if stages[-1]["exit_code"]==0 else "failed"
    manifest["model_files_after"]=_models(gsa,Path.home()); manifest["reference_adaptation"]={"path":"compat/generate_gsa_results.py","diff":"single backend selector TkAgg -> Agg for headless execution","adapted_file_sha256":det_script_hash,"source_file":"conceptgraph/scripts/generate_gsa_results.py","semantic_effect":"visualization backend only; detector/masks/features unchanged","reference_checkout_copy":"none; all other modules imported from the pinned read-only checkout"}
    (out/"run_manifest.json").write_text(json.dumps(manifest,indent=2)+"\n")
    if stages[-1]["exit_code"]!=0: return stages[-1]["exit_code"]
    native_results=sorted((work/"pilot_00808/pcd_saves").glob("*_post.pkl.gz"))
    if not native_results:
        manifest["status"]="failed"; manifest["stages"][-1]["output_validation"]={"status":"failed","reason":"native postprocessed pcd missing"}
        (out/"run_manifest.json").write_text(json.dumps(manifest,indent=2)+"\n")
        raise RuntimeError("native ConceptGraphs postprocessed map was not produced")
    stages[-1]["output_validation"]={"status":"pass","postprocessed_native_maps":len(native_results)}
    stages[-1]["output_hashes"]={str(p.relative_to(work)):sha256_file(p) for p in native_results}
    manifest["stages"]=stages; (out/"run_manifest.json").write_text(json.dumps(manifest,indent=2)+"\n")
    exported=_export_final_map(work/"pilot_00808",out,pkg,manifest)
    manifest["export_manifest_sha256"]=sha256_file(out/"export/export_manifest.json")
    validate_object_export(out/"export")
    manifest["stage_output_hashes"]={"detections_and_native_map":_file_hashes(work/"pilot_00808"),
                                     "export":_file_hashes(out/"export")}
    manifest["status"]="completed"; (out/"run_manifest.json").write_text(json.dumps(manifest,indent=2)+"\n")
    print(json.dumps({"status":"completed","mode":args.mode,"frames":end-start,"objects":len(exported["objects"]),"output":str(out),"export":str(out/"export")},indent=2))
    return 0

def main() -> int:
    p=argparse.ArgumentParser(description=__doc__); sub=p.add_subparsers(dest="cmd",required=True)
    sub.add_parser("probe",help="report current Kaggle GPU/RAM/disk/PyTorch")
    v=sub.add_parser("validate",help="validate input package"); v.add_argument("--package",type=Path,required=True)
    r=sub.add_parser("run",help="run reference detector+mapping from empty workspace"); r.add_argument("--package",type=Path,required=True); r.add_argument("--reference",type=Path,required=True); r.add_argument("--gsa",type=Path,required=True); r.add_argument("--output",type=Path,required=True); r.add_argument("--mode",choices=["smoke","full"],required=True)
    a=p.parse_args()
    if a.cmd=="probe": print(json.dumps(_probe(),indent=2)); return 0
    if a.cmd=="validate":
        m=validate_package(a.package); print(json.dumps({"status":"valid","frames":len(m["frames"]),"content_sha256":m["content_sha256"]},indent=2)); return 0
    return _run(a)
if __name__=="__main__":
    try: raise SystemExit(main())
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}",file=sys.stderr); traceback.print_exc(); raise
