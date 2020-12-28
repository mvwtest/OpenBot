import asyncio
import glob
import os
import shutil
import threading

from aiohttp import web
from aiohttp_json_rpc import JsonRpc
import numpy as np
from numpyencoder import NumpyEncoder

from .dataset import get_dataset_list, get_dir_info, get_info
from .models import get_models, models_dir
from .preview import handle_preview
from .upload import handle_file_upload
from .. import base_dir, dataset_dir
from ..train import CancelledException, Hyperparameters, MyCallback, start_train

event_cancelled = threading.Event()
json_encoder = NumpyEncoder()
rpc = JsonRpc()


async def handle_test(_: web.Request):
    return web.json_response({"openbot": 1})


async def handle_models(request: web.Request) -> web.StreamResponse:
    path = request.match_info.get("path")
    if path[-7:] == ".tflite":
        real = os.path.join(base_dir, path)
        if os.path.isfile(real):
            return web.FileResponse(real)

    models = [
        dict(name=os.path.basename(p), mtime=int(os.path.getmtime(p)))
        for p in glob.glob(os.path.join(models_dir, "*.tflite"))
    ]
    return web.json_response(models)


async def handle_upload(request: web.Request) -> web.Response:
    reader = await request.multipart()
    while not reader.at_eof():
        field = await reader.next()
        if field.name == "file":
            res = await handle_file_upload(field)
            await rpc.notify("session")
            return res

    return web.Response(text="file not found")


async def init_api(app: web.Application):
    app.router.add_get("/test", handle_test)
    app.router.add_get("/{path:models.*}", handle_models)
    app.router.add_post("/upload", handle_upload)
    app.router.add_get("/{path:.*/preview.gif}", handle_preview)
    app.router.add_route("*", "/ws", rpc.handle_request)

    rpc.add_methods(
        ("", listDir),
        ("", getDatasets),
        ("", getModels),
        ("", getHyperparameters),
        ("", moveSession),
        ("", deleteSession),
        ("", start),
        ("", stop),
    )
    rpc.add_topics(
        "session",
        "training",
    )


def listDir(params):
    path = params["path"].lstrip("/")
    basename = os.path.basename(path.rstrip("/"))
    dir_path = os.path.dirname(path.rstrip("/"))
    return dict(
        path=path,
        session=get_info(dir_path + "/", basename),
        file_list=get_dir_info(path),
    )


async def moveSession(params):
    basename = os.path.basename(params["path"])
    src = os.path.join(dataset_dir + params["path"])
    dst = os.path.join(dataset_dir + params["new_path"], basename)
    os.rename(src, dst)
    await rpc.notify("session")
    return True


async def deleteSession(params):
    real_dir = dataset_dir + params["path"]
    shutil.rmtree(real_dir)
    await rpc.notify("session")
    return True


def stop():
    event_cancelled.set()
    return True


def getDatasets():
    return dict(
        train=get_dataset_list("train_data"),
        test=get_dataset_list("test_data"),
    )


def getModels():
    return get_models()


def getHyperparameters():
    return Hyperparameters().__dict__


async def start(params):
    event_cancelled.clear()
    loop = asyncio.get_event_loop()

    def broadcast(event, payload=None):
        print("broadcast", event, payload)
        if payload:
            payload = encode(payload)
        data = dict(event=event, payload=payload)
        asyncio.run_coroutine_threadsafe(rpc.notify("training", data), loop).result()

    hyper_params = Hyperparameters()
    for p in params:
        setattr(hyper_params, p, params[p])
    print(hyper_params.__dict__)
    loop.run_in_executor(None, train, hyper_params, broadcast, event_cancelled)
    return True


def train(params, broadcast, cancelled):
    try:
        broadcast("started", params.__dict__)
        my_callback = MyCallback(broadcast, cancelled)
        tr = start_train(params, my_callback)
        broadcast("done", {"model": tr.model_name})
    except CancelledException:
        broadcast("cancelled")
    except Exception as e:
        broadcast("failed", str(e))
        raise e


def encode(obj):
    int_ = (np.int_, np.intc, np.intp, np.int8, np.int16, np.int32, np.int64)
    uint_ = (np.uint8, np.uint16, np.uint32, np.uint64)
    if isinstance(obj, int_) or isinstance(obj, uint_):
        return int(obj)

    elif isinstance(obj, (np.float_, np.float16, np.float32, np.float64)):
        return float(obj)

    elif isinstance(obj, (np.complex_, np.complex64, np.complex128)):
        return dict(real=obj.real, imag=obj.imag)

    elif isinstance(obj, (np.ndarray,)):
        return obj.tolist()

    elif isinstance(obj, (np.bool_)):
        return bool(obj)

    elif isinstance(obj, (np.void)):
        return None

    if isinstance(obj, dict):
        for k in obj:
            obj[k] = encode(obj[k])

    return obj
