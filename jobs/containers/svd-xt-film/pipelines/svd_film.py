import torch
from diffusers import StableVideoDiffusionPipeline
from pipelines.film import FILMPipeline
from pipelines.util import ListReader, VideoWriter
from PIL import Image
import einops


class StableVideoDiffusionFILMPipeline:
    def __init__(
        self,
        cache_dir: str,
        svd_config: dict = {
            "sfast": False,
            "quantize": False,
            "no_fusion": False,
        },
    ):
        repo_id = "stabilityai/stable-video-diffusion-img2vid-xt"
        self.svd_xt_pipeline = StableVideoDiffusionPipeline.from_pretrained(
            repo_id, cache_dir=cache_dir, variant="fp16", torch_dtype=torch.float16
        )
        self.svd_xt_pipeline = self.svd_xt_pipeline.to("cuda")

        if svd_config["quantize"]:
            from diffusers.utils import USE_PEFT_BACKEND

            assert USE_PEFT_BACKEND
            self.svd_xt_pipeline.unet = torch.quantization.quantize_dynamic(
                self.svd_xt_pipeline.unet,
                {torch.nn.Linear},
                dtype=torch.qint8,
                inplace=True,
            )

        if svd_config["no_fusion"]:
            torch.jit.set_fusion_strategy([("STATIC", 0), ("DYNAMIC", 0)])

        if svd_config["sfast"]:
            from pipelines.sfast import compile_model

            self.svd_xt_pipeline = compile_model(self.svd_xt_pipeline)

        self.film_pipeline = FILMPipeline(f"{cache_dir}/film_net_fp16.pt")
        self.film_pipeline = self.film_pipeline.to(device="cuda", dtype=torch.float16)

    def __call__(
        self,
        output_path: str,
        image: str,
        motion_bucket_id: float = 127,
        noise_aug_strength: float = 0.02,
        inter_frames: int = 2
    ):
        generator = torch.manual_seed(42)

        frames = self.svd_xt_pipeline(
            Image.open(image).convert("RGB"),
            decode_chunk_size=8,
            generator=generator,
            motion_bucket_id=motion_bucket_id,
            noise_aug_strength=noise_aug_strength,
            output_type="np",
        ).frames[0]

        frames = [torch.from_numpy(frame) for frame in frames]
        frames = einops.rearrange(frames, "n h w c -> n c h w")

        # 12 fps for 25 frames -> ~2s video
        fps = 12.0

        if inter_frames > 0:
            tot_frames = 25 + (25 // 2) * inter_frames
            fps = tot_frames // 2

            reader = ListReader(frames)
            frames = self.film_pipeline(reader, inter_frames=inter_frames)

        if output_path is not None:
            reader = ListReader(frames)
            height, width = reader.get_resolution()
            writer = VideoWriter(
                output_path=output_path,
                height=height,
                width=width,
                fps=fps,
                format="rgb24",
            )

            writer.open()

            for frame in frames:
                writer.write_frame(frame)

            writer.close()
