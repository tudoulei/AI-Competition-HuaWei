bucket_name=ai-competition-$USER

if [ $1 -eq 0 ]; then
    # 创建新的bucket
    ./obsutil mb obs://${bucket_name} -acl=private -location=cn-north-4
    # 创建新的文件夹
    ./obsutil mkdir obs://${bucket_name}/model_snapshots
fi

if [ $1 -eq -1 ]; then
    # 删除bucket
    ./obsutil abort obs://${bucket_name} -r -f
    ./obsutil rm obs://${bucket_name} -r -f
    ./obsutil rm obs://${bucket_name} -f
fi

# 上传model文件夹，并且采用增量上传的方式，上传每个文件时会对比桶中对应路径的对象，仅在对象不存在，
#　或者对象大小与文件大小不一致，或者对象的最后修改时间早于文件的最后修改时间时进行上传。
./obsutil cp ./model obs://${bucket_name}/model_snapshots -r -f -u -acl=private 