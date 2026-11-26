package io.github.kimkyum.scan2svg

import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import ai.onnxruntime.OrtSession
import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.RandomAccessFile
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.channels.FileChannel

/**
 * 수식 모델(ONNX)을 돌리는 범용 실행기. 무엇을 넣고 꺼낼지는 파이썬(math_ocr.py)이 JSON으로 정한다.
 *  요청: {"model": "mfr_encoder", "inputs": {이름: {"file"|"data"|"ref", "shape", "dtype"}},
 *        "outputs": [{"name", "file"} | {"name", "keep": 키} | {"name", "mode": "argmax_last"}]}
 *  응답: {"outputs": {이름: {"shape", "file"} | {"kept": true} | {"argmax": n}}} 또는 {"error": "..."}
 * 큰 배열은 파일(리틀 엔디언 float32/int64)로 주고받는다. "keep"한 출력은 메모리에 두고 다음 요청에서 "ref"로 쓴다
 * (수식 읽기에서 인코더 결과를 글자마다 다시 넘기지 않으려고).
 */
object Ort {
    private val env: OrtEnvironment by lazy { OrtEnvironment.getEnvironment() }
    private val sessions = HashMap<String, OrtSession>()
    private val kept = HashMap<String, OnnxTensor>()

    /** 앱에 든 모델(assets/models)을 파일로 풀어 둔 폴더 */
    fun modelDir(ctx: Context): File {
        val dir = File(ctx.filesDir, "models").apply { mkdirs() }
        val stamp = File(dir, ".stamp")
        val version = try {
            ctx.packageManager.getPackageInfo(ctx.packageName, 0).lastUpdateTime.toString()
        } catch (e: Exception) {
            "0"
        }
        synchronized(this) {
            if (!stamp.exists() || stamp.readText() != version) {
                close()
                dir.listFiles()?.forEach { it.delete() }
                for (name in ctx.assets.list("models") ?: emptyArray()) {
                    ctx.assets.open("models/$name").use { i -> File(dir, name).outputStream().use { i.copyTo(it) } }
                }
                stamp.writeText(version)
            }
        }
        return dir
    }

    fun available(ctx: Context): Boolean =
        (ctx.assets.list("models") ?: emptyArray()).contains("mfd.onnx")

    @Synchronized
    fun close() {
        sessions.values.forEach { try { it.close() } catch (e: Exception) {} }
        sessions.clear()
        kept.values.forEach { it.close() }
        kept.clear()
    }

    private fun session(ctx: Context, name: String): OrtSession = sessions.getOrPut(name) {
        val opts = OrtSession.SessionOptions()
        opts.setIntraOpNumThreads(4)
        opts.setOptimizationLevel(OrtSession.SessionOptions.OptLevel.ALL_OPT)
        env.createSession(File(modelDir(ctx), "$name.onnx").absolutePath, opts)
    }

    private fun readBuf(path: String): ByteBuffer {
        RandomAccessFile(path, "r").use { f ->
            val b = ByteBuffer.allocateDirect(f.length().toInt()).order(ByteOrder.LITTLE_ENDIAN)
            f.channel.read(b)
            b.rewind()
            return b
        }
    }

    private fun shapeOf(a: JSONArray) = LongArray(a.length()) { a.getLong(it) }

    private fun tensor(spec: JSONObject): OnnxTensor {
        val shape = shapeOf(spec.getJSONArray("shape"))
        val int64 = spec.optString("dtype") == "int64"
        if (spec.has("data")) {
            val d = spec.getJSONArray("data")
            return if (int64) OnnxTensor.createTensor(env, java.nio.LongBuffer.wrap(LongArray(d.length()) { d.getLong(it) }), shape)
            else OnnxTensor.createTensor(env, java.nio.FloatBuffer.wrap(FloatArray(d.length()) { d.getDouble(it).toFloat() }), shape)
        }
        val b = readBuf(spec.getString("file"))
        return if (int64) OnnxTensor.createTensor(env, b.asLongBuffer(), shape)
        else OnnxTensor.createTensor(env, b.asFloatBuffer(), shape)
    }

    @Synchronized
    fun run(ctx: Context, request: String): String = try {
        val req = JSONObject(request)
        val sess = session(ctx, req.getString("model"))
        val ins = req.getJSONObject("inputs")
        val feeds = HashMap<String, OnnxTensor>()
        val owned = ArrayList<OnnxTensor>()
        for (k in ins.keys()) {
            val spec = ins.getJSONObject(k)
            if (spec.has("ref")) {
                feeds[k] = kept[spec.getString("ref")] ?: throw IllegalStateException("없는 참조: " + spec.getString("ref"))
            } else {
                val t = tensor(spec)
                owned.add(t)
                feeds[k] = t
            }
        }
        val outs = req.getJSONArray("outputs")
        val want = (0 until outs.length()).map { outs.getJSONObject(it).getString("name") }.toSet()
        val res = JSONObject()
        sess.run(feeds, want).use { r ->
            for (i in 0 until outs.length()) {
                val o = outs.getJSONObject(i)
                val name = o.getString("name")
                val t = r.get(name).get() as OnnxTensor
                val shape = t.info.shape
                val fb = t.floatBuffer
                when {
                    o.has("keep") -> {
                        val copy = java.nio.FloatBuffer.allocate(fb.remaining())
                        copy.put(fb); copy.rewind()
                        kept.remove(o.getString("keep"))?.close()
                        kept[o.getString("keep")] = OnnxTensor.createTensor(env, copy, shape)
                        res.put(name, JSONObject().put("kept", true))
                    }
                    o.optString("mode") == "argmax_last" -> {
                        val v = shape[shape.size - 1].toInt()
                        val start = fb.remaining() - v
                        var best = 0
                        var bv = Float.NEGATIVE_INFINITY
                        for (j in 0 until v) {
                            val x = fb.get(start + j)
                            if (x > bv) { bv = x; best = j }
                        }
                        res.put(name, JSONObject().put("argmax", best))
                    }
                    else -> {
                        val bb = ByteBuffer.allocate(fb.remaining() * 4).order(ByteOrder.LITTLE_ENDIAN)
                        bb.asFloatBuffer().put(fb)
                        RandomAccessFile(o.getString("file"), "rw").use { f ->
                            f.setLength(0)
                            f.channel.write(bb)
                        }
                        res.put(name, JSONObject().put("shape", JSONArray(shape.toList())).put("file", o.getString("file")))
                    }
                }
            }
        }
        owned.forEach { it.close() }
        JSONObject().put("outputs", res).toString()
    } catch (e: Throwable) {
        android.util.Log.e(Pipeline.TAG, "ort failed", e)
        JSONObject().put("error", e.toString()).toString()
    }
}
